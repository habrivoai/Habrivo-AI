
from flask import Flask, request, jsonify, session, send_from_directory
from flask_cors import CORS
import sqlite3, os
from werkzeug.utils import secure_filename

import os
from dotenv import load_dotenv


load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
FRONTEND = os.path.join(ROOT_DIR, "frontend")
DB = os.path.join(BASE_DIR, "habrivo.db")
UPLOAD = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD, exist_ok=True)

app = Flask(__name__, static_folder=FRONTEND, static_url_path="")
app.secret_key = os.getenv("SECRET_KEY")
app.config["UPLOAD_FOLDER"] = UPLOAD

CORS(app, supports_credentials=True)

def conn():
    
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


    
    
   

with conn() as db:
    db.execute("""
CREATE TABLE IF NOT EXISTS companies(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT,
    owner TEXT,
    whatsapp TEXT,
    email TEXT,
    category TEXT,
    logo TEXT
)
""")
    db.commit()
    cols = [r["name"] for r in db.execute("PRAGMA table_info(companies)")]

if "address" not in cols:
    db.execute("ALTER TABLE companies ADD COLUMN address TEXT")

if "website" not in cols:
    db.execute("ALTER TABLE companies ADD COLUMN website TEXT")

db.commit()   
   

        
       
with conn() as db:
    cols = [r["name"] for r in db.execute("PRAGMA table_info(companies)")]

    if "address" not in cols:
        db.execute("ALTER TABLE companies ADD COLUMN address TEXT")

    if "website" not in cols:
        db.execute("ALTER TABLE companies ADD COLUMN website TEXT")

    db.commit()        

@app.get("/api/check-auth")
def check_auth():
    return jsonify({"loggedIn": session.get("admin", False)})
@app.route("/")
def home():
    return send_from_directory(FRONTEND, "index.html")

import os
from flask import request, jsonify, session

@app.post("/api/login")
def login():
    data = request.get_json() or {}

    env_email = (os.getenv("ADMIN_EMAIL") or "").strip()
    env_password = (os.getenv("ADMIN_PASSWORD") or "").strip()

    user_email = (data.get("email") or "").strip()
    user_password = (data.get("password") or "").strip()
   

    if user_email == env_email and user_password == env_password:
        session["admin"] = True
        return jsonify({"success": True})

    return jsonify({
        "success": False,
        "message": "Invalid Credentials"
    }), 401

@app.get("/api/logout")
def logout():
    session.clear()
    return jsonify({"success": True})

@app.get("/api/companies")
def companies():
    with conn() as db:
        rows=[dict(r) for r in db.execute("SELECT * FROM companies ORDER BY id DESC")]
    return jsonify(rows)


@app.post("/api/company")
def add_company():
    company = request.form.get("company", "")
    owner = request.form.get("owner", "")
    whatsapp = request.form.get("whatsapp", "")
    email = request.form.get("email", "")
    category = request.form.get("category", "")
    address = request.form.get("address", "")
    website = request.form.get("website", "")

    logo = ""
    f = request.files.get("logo")

    if f and f.filename:
        name = secure_filename(f.filename)
        f.save(os.path.join(app.config["UPLOAD_FOLDER"], name))
        logo = f"/uploads/{name}"

    with conn() as db:
        db.execute("""
            INSERT INTO companies
            (company, owner, whatsapp, email, category, address, website, logo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            company,
            owner,
            whatsapp,
            email,
            category,
            address,
            website,
            logo
        ))
        db.commit()

    return jsonify({"success": True, "logo": logo})

@app.delete("/api/company/<int:id>")
def delete_company(id):
    with conn() as db:
        db.execute("DELETE FROM companies WHERE id=?", (id,))
        db.commit()

    return jsonify({"success": True, "message": "Deleted Successfully"})
@app.put("/api/company/<int:id>")
def update_company(id):
    with conn() as db:
        db.execute("""
            UPDATE companies
           SET company=?, owner=?, whatsapp=?, category=?, address=?, website=?, logo=?
            WHERE id=?
        """, (
            request.form.get("company", ""),
            request.form.get("owner", ""),
            request.form.get("whatsapp", ""),
            request.form.get("category", ""),
            id
        ))
        db.commit()

    return jsonify({"success": True, "message": "Updated Successfully"})

@app.route("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(UPLOAD, name)

if __name__=="__main__":
    app.run(host="127.0.0.1",port=5000,debug=True)
