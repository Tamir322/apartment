import os
import datetime
from contextlib import asynccontextmanager
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, DateTime, ForeignKey, event, desc
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ===================== DATABASE ENGINE FACTORY =====================
def get_configured_engine(raw_url: Optional[str] = None):
    url = (raw_url or os.getenv("DATABASE_URL") or "").strip()
    if url:
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        try:
            connect_args = {"connect_timeout": 8} if "postgresql" in url else {}
            eng = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
            return eng
        except Exception as e:
            print(f"[!] Failed to configure PostgreSQL engine: {e}")

    # Fallback to local SQLite database
    sqlite_path = os.path.join(BASE_DIR, "plants_tracker.db")
    fallback_url = f"sqlite:///{sqlite_path}"
    eng = create_engine(fallback_url, connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.close()

    return eng


engine = get_configured_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ===================== SQLALCHEMY MODELS =====================
class Plant(Base):
    __tablename__ = "plants"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String, nullable=False)
    species_or_type = Column(String, nullable=True)
    location_group = Column(String, nullable=False, default="Apartment")
    pot_size_liters = Column(Float, nullable=True)
    growth_stage = Column(String, nullable=False, default="Vegetative")
    initial_interval_hours = Column(Float, nullable=False, default=72.0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    watering_logs = relationship(
        "WateringLog",
        back_populates="plant",
        cascade="all, delete-orphan",
        order_by="WateringLog.watered_at.asc()"
    )


class WateringLog(Base):
    __tablename__ = "watering_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    plant_id = Column(Integer, ForeignKey("plants.id", ondelete="CASCADE"), nullable=False, index=True)
    watered_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    soil_state = Column(String, nullable=False)  # "damp" | "ideal" | "bone_dry"
    raw_elapsed_hours = Column(Float, nullable=True)
    effective_interval_hours = Column(Float, nullable=True)
    notes = Column(String, nullable=True)

    plant = relationship("Plant", back_populates="watering_logs")


# ===================== DATABASE HELPER & INIT =====================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def recompute_plant_intervals(plant_id: int, db: Session):
    """Recomputes raw_elapsed_hours and effective_interval_hours for all logs of a plant."""
    logs = (
        db.query(WateringLog)
        .filter(WateringLog.plant_id == plant_id)
        .order_by(WateringLog.watered_at.asc())
        .all()
    )
    prev_log = None
    for log in logs:
        if prev_log is None:
            log.raw_elapsed_hours = None
            log.effective_interval_hours = None
        else:
            diff_hours = (log.watered_at - prev_log.watered_at).total_seconds() / 3600.0
            log.raw_elapsed_hours = round(max(0.0, diff_hours), 2)
            
            state = (log.soil_state or "ideal").lower().strip()
            if state == "damp":
                mult = 1.20
            elif state == "bone_dry":
                mult = 0.80
            else:
                mult = 1.00
            log.effective_interval_hours = round(log.raw_elapsed_hours * mult, 2)
        prev_log = log
    db.commit()


def init_plant_db():
    """Initializes tables and optionally seeds starter plants with crash-proof fallback."""
    global engine, SessionLocal
    try:
        Base.metadata.create_all(bind=engine)
        print("[+] Primary database tables initialized successfully")
    except Exception as e:
        print(f"[!] Warning: Database connection failed ({e}).")
        print("[!] Falling back to local SQLite so the server stays alive...")
        # Fallback to local SQLite so server never crash loops
        sqlite_path = os.path.join(BASE_DIR, "plants_tracker.db")
        fallback_url = f"sqlite:///{sqlite_path}"
        engine = create_engine(fallback_url, connect_args={"check_same_thread": False})
        SessionLocal.configure(bind=engine)
        Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        count = db.query(Plant).count()
        if count == 0:
            now = datetime.datetime.utcnow()
            p1 = Plant(
                name="Strawberries (Mother)",
                species_or_type="Fragaria × ananassa",
                location_group="Terrace / Home",
                pot_size_liters=5.0,
                growth_stage="Runner / Stolon",
                initial_interval_hours=54.0,
                created_at=now - datetime.timedelta(days=14)
            )
            p2 = Plant(
                name="Monstera Deliciosa",
                species_or_type="Swiss Cheese Plant",
                location_group="Apartment",
                pot_size_liters=9.0,
                growth_stage="Vegetative",
                initial_interval_hours=168.0,
                created_at=now - datetime.timedelta(days=30)
            )
            p3 = Plant(
                name="Coleus Cutting",
                species_or_type="Plectranthus scutellarioides",
                location_group="Grow Light Zone",
                pot_size_liters=0.8,
                growth_stage="Propagating / Rooting",
                initial_interval_hours=48.0,
                created_at=now - datetime.timedelta(days=10)
            )
            db.add_all([p1, p2, p3])
            db.commit()

            # Seed logs
            l1 = WateringLog(plant_id=p1.id, watered_at=now - datetime.timedelta(days=10), soil_state="ideal")
            l2 = WateringLog(plant_id=p1.id, watered_at=now - datetime.timedelta(days=7), soil_state="ideal")
            l3 = WateringLog(plant_id=p1.id, watered_at=now - datetime.timedelta(days=4, hours=12), soil_state="ideal")
            l4 = WateringLog(plant_id=p1.id, watered_at=now - datetime.timedelta(days=2, hours=12), soil_state="damp", notes="Lots of runners")
            l5 = WateringLog(plant_id=p1.id, watered_at=now - datetime.timedelta(hours=18), soil_state="ideal", notes="Kelp feed")

            m1 = WateringLog(plant_id=p2.id, watered_at=now - datetime.timedelta(days=22), soil_state="ideal")
            m2 = WateringLog(plant_id=p2.id, watered_at=now - datetime.timedelta(days=14), soil_state="ideal")
            m3 = WateringLog(plant_id=p2.id, watered_at=now - datetime.timedelta(days=6), soil_state="damp")

            c1 = WateringLog(plant_id=p3.id, watered_at=now - datetime.timedelta(days=8), soil_state="ideal")
            c2 = WateringLog(plant_id=p3.id, watered_at=now - datetime.timedelta(days=5), soil_state="ideal")
            c3 = WateringLog(plant_id=p3.id, watered_at=now - datetime.timedelta(days=2), soil_state="bone_dry")

            db.add_all([l1, l2, l3, l4, l5, m1, m2, m3, c1, c2, c3])
            db.commit()

            recompute_plant_intervals(p1.id, db)
            recompute_plant_intervals(p2.id, db)
            recompute_plant_intervals(p3.id, db)
            print("[+] Seeded initial starter plants successfully")
    except Exception as e:
        print(f"[!] Warning during starter seed: {e}")
        db.rollback()
    finally:
        db.close()


# ===================== METRIC CALCULATION ENGINE =====================
def calculate_plant_metrics(plant: Plant, now: Optional[datetime.datetime] = None) -> dict:
    if now is None:
        now = datetime.datetime.utcnow()

    intervals = [
        l.effective_interval_hours
        for l in plant.watering_logs
        if l.effective_interval_hours is not None and l.effective_interval_hours > 0
    ]

    if not intervals:
        baseline_interval_hours = float(plant.initial_interval_hours or 72.0)
    else:
        last_10 = intervals[-10:]
        baseline_interval_hours = round(sum(last_10) / len(last_10), 2)

    if len(intervals) >= 3:
        recent_interval_hours = round(
            0.50 * intervals[-1] + 0.30 * intervals[-2] + 0.20 * intervals[-3],
            2
        )
    elif len(intervals) == 2:
        recent_interval_hours = round(
            0.625 * intervals[-1] + 0.375 * intervals[-2],
            2
        )
    elif len(intervals) == 1:
        recent_interval_hours = round(intervals[-1], 2)
    else:
        recent_interval_hours = None

    if len(intervals) >= 3 and baseline_interval_hours > 0 and recent_interval_hours is not None:
        delta = ((baseline_interval_hours - recent_interval_hours) / baseline_interval_hours) * 100.0
        drinking_speed_delta_pct = round(delta, 1)
    else:
        drinking_speed_delta_pct = 0.0

    if drinking_speed_delta_pct >= 15.0:
        trend_badge = f"▲ Drinking {int(round(drinking_speed_delta_pct))}% Faster"
        trend_class = "faster"
    elif drinking_speed_delta_pct <= -15.0:
        trend_badge = f"▼ Drinking {int(round(abs(drinking_speed_delta_pct)))}% Slower"
        trend_class = "slower"
    else:
        trend_badge = "Steady Pace"
        trend_class = "steady"

    predicted_interval_hours = (
        recent_interval_hours if recent_interval_hours is not None else baseline_interval_hours
    )
    predicted_interval_hours = max(1.0, predicted_interval_hours)

    if plant.watering_logs:
        last_log = plant.watering_logs[-1]
        last_watered_at = last_log.watered_at
        last_soil_state = last_log.soil_state
    else:
        last_watered_at = plant.created_at or now
        last_soil_state = None

    next_due_at = last_watered_at + datetime.timedelta(hours=predicted_interval_hours)
    hours_remaining = round((next_due_at - now).total_seconds() / 3600.0, 1)
    hours_since_watered = round(max(0.0, (now - last_watered_at).total_seconds() / 3600.0), 1)

    if hours_remaining <= 0:
        urgency_status = "OVERDUE"
    elif hours_remaining <= 18:
        urgency_status = "DUE_TODAY"
    else:
        urgency_status = "GOOD"

    if predicted_interval_hours > 0:
        dry_down_pct = min(100.0, max(0.0, round((hours_since_watered / predicted_interval_hours) * 100.0, 1)))
    else:
        dry_down_pct = 100.0

    predicted_days = round(predicted_interval_hours / 24.0, 1)
    if urgency_status == "OVERDUE":
        overdue_hrs = int(round(abs(hours_remaining)))
        due_text = "Overdue right now" if overdue_hrs == 0 else f"Overdue by {overdue_hrs}h (Cycle: {predicted_days}d)"
    elif urgency_status == "DUE_TODAY":
        hrs = max(0, int(round(hours_remaining)))
        due_text = f"Due today in {hrs}h (Every {predicted_days} days)"
    else:
        hrs = int(round(hours_remaining))
        due_text = f"Due in {hrs}h (Every {predicted_days} days)"

    return {
        "id": plant.id,
        "name": plant.name,
        "species_or_type": plant.species_or_type,
        "location_group": plant.location_group,
        "pot_size_liters": plant.pot_size_liters,
        "growth_stage": plant.growth_stage,
        "initial_interval_hours": plant.initial_interval_hours,
        "created_at": plant.created_at.isoformat() if plant.created_at else None,
        "last_watered_at": last_watered_at.isoformat() if last_watered_at else None,
        "last_soil_state": last_soil_state,
        "next_due_at": next_due_at.isoformat() if next_due_at else None,
        "hours_remaining": hours_remaining,
        "hours_since_watered": hours_since_watered,
        "baseline_interval_hours": baseline_interval_hours,
        "recent_interval_hours": recent_interval_hours,
        "predicted_interval_hours": round(predicted_interval_hours, 1),
        "predicted_interval_days": predicted_days,
        "drinking_speed_delta_pct": drinking_speed_delta_pct,
        "trend_badge": trend_badge,
        "trend_class": trend_class,
        "urgency_status": urgency_status,
        "dry_down_pct": dry_down_pct,
        "due_text": due_text,
        "logs_count": len(plant.watering_logs)
    }


# ===================== PYDANTIC SCHEMAS =====================
class PlantCreate(BaseModel):
    name: str = Field(..., min_length=1)
    species_or_type: Optional[str] = None
    location_group: Optional[str] = "Apartment"
    pot_size_liters: Optional[float] = None
    growth_stage: Optional[str] = "Vegetative"
    initial_interval_hours: Optional[float] = 72.0


class PlantUpdate(BaseModel):
    name: Optional[str] = None
    species_or_type: Optional[str] = None
    location_group: Optional[str] = None
    pot_size_liters: Optional[float] = None
    growth_stage: Optional[str] = None
    initial_interval_hours: Optional[float] = None


class WaterLogCreate(BaseModel):
    soil_state: str = Field(..., pattern="^(damp|ideal|bone_dry)$")
    notes: Optional[str] = None
    watered_at: Optional[datetime.datetime] = None


class WaterLogUpdate(BaseModel):
    soil_state: Optional[str] = Field(None, pattern="^(damp|ideal|bone_dry)$")
    notes: Optional[str] = None
    watered_at: Optional[datetime.datetime] = None


# ===================== FASTAPI ROUTER =====================
plants_router = APIRouter(prefix="/plants", tags=["plants"])


@plants_router.get("")
@plants_router.get("/")
def serve_plants_ui():
    """Serves the mobile-first Plant Tracker UI."""
    html_file = os.path.join(BASE_DIR, "plants_index.html")
    if not os.path.exists(html_file):
        raise HTTPException(status_code=404, detail="plants_index.html not found")
    return FileResponse(html_file)


@plants_router.get("/manifest.json")
def serve_manifest():
    return FileResponse(os.path.join(BASE_DIR, "manifest.json"), media_type="application/manifest+json")


@plants_router.get("/sw.js")
def serve_sw():
    return FileResponse(os.path.join(BASE_DIR, "sw.js"), media_type="application/javascript")


@plants_router.get("/icon-192.png")
def serve_icon_192():
    return FileResponse(os.path.join(BASE_DIR, "icon-192.png"), media_type="image/png")


@plants_router.get("/icon-512.png")
def serve_icon_512():
    return FileResponse(os.path.join(BASE_DIR, "icon-512.png"), media_type="image/png")


@plants_router.get("/api/list")
def list_plants(db: Session = Depends(get_db)):
    """Returns all plants with computed drinking velocity metrics and summary counts."""
    now = datetime.datetime.utcnow()
    plants = db.query(Plant).all()

    plant_items = [calculate_plant_metrics(p, now=now) for p in plants]

    urgency_ranks = {"OVERDUE": 0, "DUE_TODAY": 1, "GOOD": 2}
    plant_items.sort(key=lambda x: (urgency_ranks.get(x["urgency_status"], 3), x["hours_remaining"]))

    overdue_count = sum(1 for p in plant_items if p["urgency_status"] == "OVERDUE")
    due_today_count = sum(1 for p in plant_items if p["urgency_status"] == "DUE_TODAY")
    good_count = sum(1 for p in plant_items if p["urgency_status"] == "GOOD")

    default_locations = ["All", "Apartment", "Terrace / Home", "Grow Light Zone"]
    db_locations = [p.location_group for p in plants if p.location_group]
    all_locations = ["All"]
    for loc in ["Apartment", "Terrace / Home", "Grow Light Zone"]:
        if loc not in all_locations:
            all_locations.append(loc)
    for loc in db_locations:
        if loc and loc not in all_locations:
            all_locations.append(loc)

    return {
        "summary": {
            "total": len(plant_items),
            "overdue": overdue_count,
            "due_today": due_today_count,
            "good": good_count
        },
        "locations": all_locations,
        "plants": plant_items
    }


@plants_router.post("/api/plants")
def create_plant(payload: PlantCreate, db: Session = Depends(get_db)):
    plant = Plant(
        name=payload.name.strip(),
        species_or_type=payload.species_or_type.strip() if payload.species_or_type else None,
        location_group=payload.location_group.strip() if payload.location_group else "Apartment",
        pot_size_liters=payload.pot_size_liters,
        growth_stage=payload.growth_stage or "Vegetative",
        initial_interval_hours=payload.initial_interval_hours or 72.0,
        created_at=datetime.datetime.utcnow()
    )
    db.add(plant)
    db.commit()
    db.refresh(plant)
    return calculate_plant_metrics(plant)


@plants_router.get("/api/plants/{plant_id}")
def get_plant_details(plant_id: int, db: Session = Depends(get_db)):
    plant = db.query(Plant).filter(Plant.id == plant_id).first()
    if not plant:
        raise HTTPException(status_code=404, detail="Plant not found")

    metrics = calculate_plant_metrics(plant)

    logs_data = []
    for log in reversed(plant.watering_logs):
        logs_data.append({
            "id": log.id,
            "watered_at": log.watered_at.isoformat() if log.watered_at else None,
            "soil_state": log.soil_state,
            "raw_elapsed_hours": log.raw_elapsed_hours,
            "raw_elapsed_days": round(log.raw_elapsed_hours / 24.0, 1) if log.raw_elapsed_hours else None,
            "effective_interval_hours": log.effective_interval_hours,
            "effective_interval_days": round(log.effective_interval_hours / 24.0, 1) if log.effective_interval_hours else None,
            "notes": log.notes
        })

    chart_points = [
        {
            "id": log.id,
            "watered_at": log.watered_at.strftime("%b %d"),
            "effective_hours": log.effective_interval_hours,
            "effective_days": round(log.effective_interval_hours / 24.0, 1) if log.effective_interval_hours else 0,
            "soil_state": log.soil_state
        }
        for log in plant.watering_logs
        if log.effective_interval_hours is not None
    ]

    return {
        "plant": metrics,
        "logs": logs_data,
        "sparkline": chart_points
    }


@plants_router.patch("/api/plants/{plant_id}")
def update_plant(plant_id: int, payload: PlantUpdate, db: Session = Depends(get_db)):
    plant = db.query(Plant).filter(Plant.id == plant_id).first()
    if not plant:
        raise HTTPException(status_code=404, detail="Plant not found")

    if payload.name is not None:
        plant.name = payload.name.strip()
    if payload.species_or_type is not None:
        plant.species_or_type = payload.species_or_type.strip()
    if payload.location_group is not None:
        plant.location_group = payload.location_group.strip()
    if payload.pot_size_liters is not None:
        plant.pot_size_liters = payload.pot_size_liters
    if payload.growth_stage is not None:
        plant.growth_stage = payload.growth_stage
    if payload.initial_interval_hours is not None:
        plant.initial_interval_hours = payload.initial_interval_hours

    db.commit()
    db.refresh(plant)
    return calculate_plant_metrics(plant)


@plants_router.delete("/api/plants/{plant_id}")
def delete_plant(plant_id: int, db: Session = Depends(get_db)):
    plant = db.query(Plant).filter(Plant.id == plant_id).first()
    if not plant:
        raise HTTPException(status_code=404, detail="Plant not found")
    db.delete(plant)
    db.commit()
    return {"status": "ok", "message": f"Plant '{plant.name}' deleted"}


@plants_router.post("/api/water/{plant_id}")
def water_plant(plant_id: int, payload: WaterLogCreate, db: Session = Depends(get_db)):
    plant = db.query(Plant).filter(Plant.id == plant_id).first()
    if not plant:
        raise HTTPException(status_code=404, detail="Plant not found")

    watered_at = payload.watered_at or datetime.datetime.utcnow()

    new_log = WateringLog(
        plant_id=plant_id,
        watered_at=watered_at,
        soil_state=payload.soil_state.lower().strip(),
        notes=payload.notes.strip() if payload.notes else None
    )
    db.add(new_log)
    db.commit()

    recompute_plant_intervals(plant_id, db)
    db.refresh(plant)

    return {
        "status": "ok",
        "message": f"Logged watering for {plant.name}",
        "plant": calculate_plant_metrics(plant)
    }


@plants_router.put("/api/logs/{log_id}")
def edit_watering_log(log_id: int, payload: WaterLogUpdate, db: Session = Depends(get_db)):
    log = db.query(WateringLog).filter(WateringLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log entry not found")

    if payload.soil_state is not None:
        log.soil_state = payload.soil_state.lower().strip()
    if payload.notes is not None:
        log.notes = payload.notes.strip()
    if payload.watered_at is not None:
        log.watered_at = payload.watered_at

    db.commit()
    recompute_plant_intervals(log.plant_id, db)

    plant = db.query(Plant).filter(Plant.id == log.plant_id).first()
    return {
        "status": "ok",
        "message": "Watering log updated",
        "plant": calculate_plant_metrics(plant) if plant else None
    }


@plants_router.delete("/api/logs/{log_id}")
def delete_watering_log(log_id: int, db: Session = Depends(get_db)):
    log = db.query(WateringLog).filter(WateringLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log entry not found")

    plant_id = log.plant_id
    db.delete(log)
    db.commit()

    recompute_plant_intervals(plant_id, db)
    plant = db.query(Plant).filter(Plant.id == plant_id).first()
    return {
        "status": "ok",
        "message": "Watering log deleted",
        "plant": calculate_plant_metrics(plant) if plant else None
    }


# ===================== STANDALONE APP INSTANCE =====================
@asynccontextmanager
async def plant_lifespan(app: FastAPI):
    init_plant_db()
    yield


app = FastAPI(title="Tmimer's Plants", lifespan=plant_lifespan)
app.include_router(plants_router)


@app.get("/healthz")
@app.get("/health")
def healthcheck():
    return {"status": "ok", "app": "Tmimer's Plants"}


@app.get("/")
def redirect_to_plants():
    return FileResponse(os.path.join(BASE_DIR, "plants_index.html"))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"[*] Starting server on 0.0.0.0:{port}...")
    uvicorn.run("plants_app:app", host="0.0.0.0", port=port, log_level="info")
