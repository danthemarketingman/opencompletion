from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

import eventlet

import openai

from openai.error import InvalidRequestError

import os

import time

import boto3
import json


app = Flask(__name__)
app.config["SECRET_KEY"] = "your_secret_key"
socketio = SocketIO(app, async_mode="eventlet")


from flask_sqlalchemy import SQLAlchemy

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///chat.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(128), nullable=False)
    content = db.Column(db.String(1024), nullable=False)
    room = db.Column(db.String(128), nullable=False)

    def __init__(self, username, content, room):
        self.username = username
        self.content = content
        self.room = room


# Create the database and tables
with app.app_context():
    db.create_all()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/chat/<room>")
def chat(room):
    return render_template("chat.html", room=room)


@socketio.on("join")
def on_join(data):
    room = data["room"]
    join_room(room)

    # Fetch previous messages from the database
    previous_messages = Message.query.filter_by(room=room).all()

    for message in previous_messages:
        emit(
            "previous_messages",
            {"username": message.username, "message": message.content},
            room=request.sid,
        )

    emit("message", f"{data['username']} has joined the room.", room=room)


@socketio.on("message")
def handle_message(data):
    # Save the message to the database
    new_message = Message(
        username=data["username"], content=data["message"], room=data["room"]
    )
    db.session.add(new_message)
    db.session.commit()

    emit("message", f"{data['username']}: {data['message']}", room=data["room"])

    if "claude" in data["message"] or "gpt" in data["message"]: 
        # Emit a temporary message indicating that llm is processing
        emit("message", f"<span id='processing'>Processing...</span>", room=data["room"])

    if "claude" in data["message"]:
        # Call the chat_claude function without blocking using eventlet.spawn
        eventlet.spawn(chat_claude, data["username"], data["room"], data["message"])

    elif "gpt" in data["message"]:
        # Call the chat_gpt function without blocking using eventlet.spawn
        eventlet.spawn(chat_gpt, data["username"], data["room"], data["message"])


def chat_claude(username, room, message): 

    with app.app_context():
        # claude has a 100,000 token context window for prompts.
        all_messages = (
            Message.query.filter_by(room=room)
            .order_by(Message.id.desc())
            .all()
        )

    chat_history = ""

    for msg in reversed(all_messages):
        if msg.username not in ["gpt-3.5-turbo", "anthropic.claude-v2"]:
            chat_history += f"Human: {msg.username}: {msg.content}\n\n"
        else:
            chat_history += f"Assistant: {msg.content}\n\n"
    
    # append the new message.
    chat_history += f"Human: {username}: {message}\n\nAssistant:"
    

    # Initialize the Bedrock client using boto3
    client = boto3.client("bedrock-runtime", region_name="us-east-1")

    # Define the request parameters
    params = {
        "modelId": "anthropic.claude-v2",
        "contentType": "application/json",
        "accept": "*/*",
        "body": json.dumps(
            {
                "prompt": chat_history,
                "max_tokens_to_sample": 2048,
                "temperature": 0,
                "top_k": 250,
                "top_p": 0.999,
                "stop_sequences": ["\n\nHuman:"],
                "anthropic_version": "bedrock-2023-05-31",
            }
        ).encode(),
    }

    # Invoke the model with response stream
    response = client.invoke_model_with_response_stream(**params)

    # Process the event stream
    buffer = ""

    first_chunk = True
    for event in response["body"]:

        content = ""

        if "chunk" in event:
            chunk_data = json.loads(event["chunk"]["bytes"].decode())
            content = chunk_data["completion"]

        if content:
            buffer += content  # Accumulate content

            if first_chunk:
                socketio.emit("message_chunk", f"{username} (anthropic.claude-v2): {content}", room=room)
                first_chunk = False
            else:
                socketio.emit("message_chunk", content, room=room)
            socketio.sleep(0)  # Force immediate handling
 
    # Save the entire completion to the database
    with app.app_context():
        new_message = Message(username="anthropic.claude-v2", content=buffer, room=room)
        db.session.add(new_message)
        db.session.commit()

    socketio.emit("delete_processing_message", "", room=room)


def chat_gpt(username, room, message):

    with app.app_context():
        last_messages = (
            Message.query.filter_by(room=room)
            .order_by(Message.id.desc())
            .limit(10)
            .all()
        )

    chat_history = [
        {"role": "system" if (msg.username == "gpt-3.5-turbo" or msg.username == "anthropic.claude-v2") else "user", "content": f"{msg.username}: {msg.content}"}
        for msg in reversed(last_messages)
    ]

    chat_history.append({"role": "user", "content": message})

    buffer = ""  # Content buffer for accumulating the chunks

    first_chunk = True
    for chunk in openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=chat_history,
        temperature=0,
        stream=True,
    ):
        content = chunk["choices"][0].get("delta", {}).get("content")

        if content:
            buffer += content  # Accumulate content

            if first_chunk:
                socketio.emit("message_chunk", f"{username} (gpt-3.5-turbo): {content}", room=room)
                first_chunk = False
            else:
                socketio.emit("message_chunk", content, room=room)
            socketio.sleep(0)  # Force immediate handling

    # Save the entire completion to the database
    with app.app_context():
        new_message = Message(username="gpt-3.5-turbo", content=buffer, room=room)
        db.session.add(new_message)
        db.session.commit()

    socketio.emit("delete_processing_message", "", room=room)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5001)
