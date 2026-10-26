# 🚀 Cloud Deployment Guide: Plant Tracker (Render + Supabase)

This guide walks you through deploying the **Plant Watering & Consumption Velocity Tracker** module to **Render** with a **Supabase PostgreSQL** database, making it accessible from your phone or any browser anywhere in the world.

---

## 1. Supabase Database Setup

1. **Sign in / Sign up**: Go to [supabase.com](https://supabase.com) and create or open a project.
2. **Retrieve Connection String**:
   - In your Supabase dashboard, click **Project Settings** (gear icon) → **Database**.
   - Under **Connection string**, select **URI** (or **Session Pooler / Transaction Pooler**).
   - The connection string looks like:
     ```
     postgresql://postgres.[project-ref]:[YOUR-PASSWORD]@aws-0-[region].pooler.supabase.com:6543/postgres
     ```
     *(Or direct connection port `5432`)*
   - Replace `[YOUR-PASSWORD]` with your actual Supabase database password.
3. **Automatic Table Creation**:
   - You **do not** need to manually run any SQL scripts! When the application boots, SQLAlchemy executes `Base.metadata.create_all()`, automatically creating the `plants` and `watering_logs` tables and seeding initial starter plants if empty.

---

## 2. Deploy to Render

### Option A: Using `render.yaml` (Blueprint)
1. Push your repository to GitHub or GitLab.
2. In [render.com](https://dashboard.render.com), click **New +** → **Blueprint**.
3. Select your repository. Render will automatically detect `render.yaml`.
4. In the environment variable prompt, paste your Supabase connection string for `DATABASE_URL`.
5. Click **Apply**. Render will install `requirements.txt` and launch the service!

### Option B: Manual Web Service Creation
1. In Render Dashboard, click **New +** → **Web Service**.
2. Connect your Git repository.
3. Configure the following settings:
   - **Environment**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn plants_app:app --host 0.0.0.0 --port $PORT`
4. In the **Environment Variables** tab, add:
   - `DATABASE_URL` = `postgresql://postgres.[project-ref]:[YOUR-PASSWORD]@aws-0-[region].pooler.supabase.com:6543/postgres`
5. Click **Deploy Web Service**.

> **Note on URL schemes**: If your connection string starts with `postgres://`, `plants_app.py` automatically normalizes it to `postgresql://` so SQLAlchemy and `psycopg2` connect cleanly without error.

---

## 3. Accessing from Your Mobile Phone

1. Once Render finishes deployment, your app will have a live HTTPS URL:
   ```
   https://plant-watering-tracker.onrender.com/plants
   ```
2. Open that URL on your mobile phone browser (Chrome on Android, Safari on iOS).
3. **PWA / Add to Home Screen**:
   - **Android (Chrome)**: Tap the 3-dot menu → **"Install app"** or **"Add to Home screen"**.
   - **iOS (Safari)**: Tap the Share button → **"Add to Home Screen"**.
4. You now have a standalone, full-screen mobile app icon on your phone!

---

## 4. Local Execution (Hybrid Mode)

When developing or running locally on your home network:
- If `DATABASE_URL` is **not set**, the app automatically falls back to a high-performance local SQLite database (`plants_tracker.db` in WAL mode).
- Run the full modular server (Chess Trainer + Plant Tracker):
  ```bash
  python app.py
  ```
- Or run only the Plant Tracker:
  ```bash
  python plants_app.py
  ```
- On startup, the console displays your local LAN IP (e.g. `http://192.168.1.xxx:8000/plants`) so devices on your home Wi-Fi can connect without any internet dependency.
