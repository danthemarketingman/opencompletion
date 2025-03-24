import os
import gevent
from gevent import monkey

# Monkey-patch at the very beginning to avoid SSL issues
monkey.patch_all()

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline

app = Flask(__name__)
app.config["SECRET_KEY"] = "your_secret_key"
app.config["SQLALCHEMY_DATABASE_URI"] = "postgresql://chat_db_ndcz_user:7qu62Pvk3JKuub0fHdPc1hJoRHlfcBPf@dpg-cvgolhiqgecs73f04nh0-a/chat_db_ndcz"  # Replace with your Render Postgres URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)
socketio = SocketIO(app, async_mode="gevent")

# Define database models
class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    active_users = db.Column(db.JSON, default=[])
    inactive_users = db.Column(db.JSON, default=[])

    def add_user(self, username):
        if username not in self.active_users:
            self.active_users.append(username)
            db.session.commit()

    def remove_user(self, username):
        if username in self.active_users:
            self.active_users.remove(username)
            if username not in self.inactive_users:
                self.inactive_users.append(username)
            db.session.commit()

    def get_active_users(self):
        return self.active_users

    def get_inactive_users(self):
        return self.inactive_users

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), nullable=False)
    content = db.Column(db.Text, nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey('room.id'), nullable=False)

# Set the cache directory for transformers
os.environ["TRANSFORMERS_CACHE"] = "/opt/server/transformers_cache"

# Load a local model (distilgpt2 for simplicity)
model_name = "distilgpt2"
try:
    print(f"Loading tokenizer for {model_name}...")
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    print(f"Tokenizer loaded successfully.")
    print(f"Loading model for {model_name}...")
    model = AutoModelForCausalLM.from_pretrained(model_name)
    print(f"Model loaded successfully.")
    nlp = pipeline("text-generation", model=model, tokenizer=tokenizer, max_new_tokens=50)
    print("Pipeline initialized successfully.")
except Exception as e:
    print(f"Failed to load model or tokenizer: {str(e)}")
    raise

def get_room(room_name):
    room = Room.query.filter_by(name=room_name).first()
    if not room:
        room = Room(name=room_name)
        db.session.add(room)
        db.session.commit()
    return room

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/chat/<room_name>")
def chat(room_name):
    rooms = Room.query.order_by(Room.id.desc()).all()
    username = request.args.get("username", "guest")
    return render_template("chat.html", room_name=room_name, rooms=rooms, username=username)

@socketio.on("join")
def on_join(data):
    room_name = data["room_name"]
    username = data["username"]
    room = get_room(room_name)

    room.add_user(username)
    join_room(room_name)

    emit("active_users", {
        "active_users": room.get_active_users(),
        "inactive_users": room.get_inactive_users()
    }, room=room_name)

    previous_messages = Message.query.filter_by(room_id=room.id).all()
    for message in previous_messages:
        emit("previous_messages", {
            "id": message.id,
            "username": message.username,
            "content": message.content
        }, room=request.sid)

    emit("chat_message", {
        "id": None,
        "content": f"{username} has joined the room."
    }, room=room_name)

@socketio.on("chat_message")
def handle_message(data):
    room_name = data["room_name"]
    username = data["username"]
    message = data["message"].strip()

    room = get_room(room_name)
    new_message = Message(username=username, content=message, room_id=room.id)
    db.session.add(new_message)
    db.session.commit()

    emit("chat_message", {
        "id": new_message.id,
        "username": username,
        "content": message
    }, room=room_name)

    # Generate AI response
    last_messages = Message.query.filter_by(room_id=room.id).order_by(Message.id.desc()).limit(5).all()
    context = " ".join([msg.content for msg in reversed(last_messages)])
    prompt = f"{context} User: {message} AI: "
    response = nlp(prompt)[0]['generated_text'].split("AI: ")[-1]

    ai_message = Message(username="AI", content=response, room_id=room.id)
    db.session.add(ai_message)
    db.session.commit()

    emit("chat_message", {
        "id": ai_message.id,
        "username": "AI",
        "content": response
    }, room=room_name)

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    socketio.run(app, host="0.0.0.0", port=5000)
