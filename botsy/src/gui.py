#!/usr/bin/env python3
"""
gui.py – A web GUI for monitoring and commanding Botsy’s bots.

This script starts a Flask web server on localhost:8760. It displays a sleek,
futuristic dashboard with menus, buttons, and a console at the bottom so that
you can see the status of all bots and send commands to them.

It requires that the main Botsy process calls set_bots(bots_dict) so that this
GUI has access to the live bots.
"""

import threading
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# Global variable that will store the bots registry.
bot_registry = {}

def set_bots(bots):
    """Called from the main Botsy process to supply the live bots."""
    global bot_registry
    bot_registry = bots

# ------------------------------
# HTML Templates (inline for demo)
# ------------------------------

dashboard_template = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Botsy Dashboard</title>
  <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
  <style>
    body {
      background: #121212;
      color: #f0f0f0;
      font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    }
    .navbar {
      background-color: #1f1f1f;
    }
    .card {
      background: #1e1e1e;
      border: none;
      margin-bottom: 20px;
    }
    .console {
      background: #000;
      color: #0f0;
      font-family: monospace;
      height: 150px;
      overflow-y: scroll;
      padding: 10px;
    }
    .console-input {
      background: #1f1f1f;
      border: none;
      color: #0f0;
      width: 100%;
      padding: 10px;
      font-family: monospace;
    }
    .futuristic-header {
      font-size: 2rem;
      color: #4caf50;
      margin-bottom: 20px;
    }
  </style>
</head>
<body>
  <nav class="navbar navbar-expand-lg">
    <a class="navbar-brand" href="/">Botsy GUI</a>
    <div class="collapse navbar-collapse">
      <ul class="navbar-nav mr-auto">
        <li class="nav-item"><a class="nav-link" href="/dashboard">Dashboard</a></li>
        <li class="nav-item"><a class="nav-link" href="/bots">Bots</a></li>
      </ul>
    </div>
  </nav>
  <div class="container mt-4">
    <h1 class="futuristic-header">Dashboard</h1>
    <div class="row">
      {% for bot in bots %}
      <div class="col-md-4">
        <div class="card">
          <div class="card-body">
            <h5 class="card-title">{{ bot.name }}</h5>
            <p>Status: <strong>{{ bot.status }}</strong></p>
            <p>Mood: <strong>{{ bot.mood }}</strong></p>
            <a href="/bot/{{ bot.name }}" class="btn btn-outline-success btn-sm">View Details</a>
          </div>
        </div>
      </div>
      {% endfor %}
    </div>
  </div>
  <!-- Console at bottom -->
  <div class="container-fluid fixed-bottom" style="background:#1f1f1f; padding:10px;">
    <div class="console" id="console-output"></div>
    <input type="text" id="console-input" class="console-input" placeholder="Type a command here... (e.g. start bot1)" onkeydown="if(event.key==='Enter') sendCommand();">
  </div>
  <script>
    function sendCommand(){
      let input = document.getElementById("console-input");
      let cmd = input.value;
      // For demo, assume command is in the format: bot_name: command_text
      let parts = cmd.split(":");
      if(parts.length < 2){
        appendToConsole("Invalid command format. Use: bot_name: command");
        return;
      }
      let bot = parts[0].trim();
      let command = parts.slice(1).join(":").trim();
      fetch("/api/command", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({"bot": bot, "command": command})
      }).then(response => response.json()).then(data => {
        appendToConsole("Command sent to " + bot + ": " + command);
        input.value = "";
      }).catch(err => {
        appendToConsole("Error sending command: " + err);
      });
    }
    function appendToConsole(text){
      let output = document.getElementById("console-output");
      output.innerHTML += text + "<br>";
      output.scrollTop = output.scrollHeight;
    }
  </script>
</body>
</html>
"""

bots_template = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Bots List</title>
  <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
  <style>
    body { background: #121212; color: #f0f0f0; }
    .navbar { background-color: #1f1f1f; }
    .card { background: #1e1e1e; border: none; margin-bottom: 20px; }
  </style>
</head>
<body>
  <nav class="navbar navbar-expand-lg">
    <a class="navbar-brand" href="/">Botsy GUI</a>
    <div class="collapse navbar-collapse">
      <ul class="navbar-nav mr-auto">
        <li class="nav-item"><a class="nav-link" href="/dashboard">Dashboard</a></li>
        <li class="nav-item"><a class="nav-link" href="/bots">Bots</a></li>
      </ul>
    </div>
  </nav>
  <div class="container mt-4">
    <h1>Bots</h1>
    <table class="table table-dark table-striped">
      <thead>
        <tr><th>Name</th><th>Status</th><th>Mood</th><th>Actions</th></tr>
      </thead>
      <tbody>
        {% for bot in bots %}
        <tr>
          <td>{{ bot.name }}</td>
          <td>{{ bot.status }}</td>
          <td>{{ bot.mood }}</td>
          <td><a href="/bot/{{ bot.name }}" class="btn btn-outline-success btn-sm">Details</a></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</body>
</html>
"""

bot_detail_template = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{{ bot_name }} Details</title>
  <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
  <style>
    body { background: #121212; color: #f0f0f0; }
    .navbar { background-color: #1f1f1f; }
    .console { background: #000; color: #0f0; font-family: monospace; height: 150px; overflow-y: scroll; padding: 10px; }
    .console-input { background: #1f1f1f; border: none; color: #0f0; width: 100%; padding: 10px; font-family: monospace; }
  </style>
</head>
<body>
  <nav class="navbar navbar-expand-lg">
    <a class="navbar-brand" href="/">Botsy GUI</a>
    <div class="collapse navbar-collapse">
      <ul class="navbar-nav mr-auto">
        <li class="nav-item"><a class="nav-link" href="/dashboard">Dashboard</a></li>
        <li class="nav-item"><a class="nav-link" href="/bots">Bots</a></li>
      </ul>
    </div>
  </nav>
  <div class="container mt-4">
    <h1>Bot: {{ bot_name }}</h1>
    <p>Status: <strong>{{ status }}</strong></p>
    <p>Mood: <strong>{{ mood }}</strong></p>
    <h3>Log</h3>
    <pre style="background:#000; color:#0f0; padding:10px;">{{ log }}</pre>
    <h3>Command Console</h3>
    <div class="form-group">
      <input type="text" id="bot-console-input" class="form-control" placeholder="Enter command for {{ bot_name }}..." onkeydown="if(event.key==='Enter') sendBotCommand();">
    </div>
    <div id="bot-console-output" class="console"></div>
  </div>
  <script>
    function sendBotCommand(){
      let input = document.getElementById("bot-console-input");
      let command = input.value;
      fetch("/api/command", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({"bot": "{{ bot_name }}", "command": command})
      }).then(response => response.json()).then(data => {
        appendToBotConsole("Command executed: " + command);
        input.value = "";
      }).catch(err => {
        appendToBotConsole("Error: " + err);
      });
    }
    function appendToBotConsole(text){
      let output = document.getElementById("bot-console-output");
      output.innerHTML += text + "<br>";
      output.scrollTop = output.scrollHeight;
    }
  </script>
</body>
</html>
"""

app = Flask(__name__)

@app.route("/")
def index():
    return dashboard()

@app.route("/dashboard")
def dashboard():
    bots_data = []
    for name, bot in bot_registry.items():
        bots_data.append({
            "name": name,
            "status": bot.get_status(),
            "mood": bot.mood_state
        })
    return render_template_string(dashboard_template, bots=bots_data)

@app.route("/bots")
def bots_list():
    bots_data = []
    for name, bot in bot_registry.items():
        bots_data.append({
            "name": name,
            "status": bot.get_status(),
            "mood": bot.mood_state
        })
    return render_template_string(bots_template, bots=bots_data)

@app.route("/bot/<bot_name>")
def bot_detail(bot_name):
    bot = bot_registry.get(bot_name)
    if not bot:
        return "Bot not found", 404
    # For simplicity, using a placeholder log.
    log_output = "Sample log output for " + bot_name
    return render_template_string(bot_detail_template,
                                  bot_name=bot_name,
                                  status=bot.get_status(),
                                  mood=bot.mood_state,
                                  log=log_output)

@app.route("/api/command", methods=["POST"])
def api_command():
    data = request.get_json()
    bot_name = data.get("bot")
    command = data.get("command")
    if bot_name not in bot_registry:
        return jsonify({"error": "Bot not found"}), 404
    bot = bot_registry[bot_name]
    try:
        bot.process_console_command(command)
        return jsonify({"status": "Command executed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def run_gui():
    app.run(port=8760, debug=False, use_reloader=False)