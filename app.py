import sqlite3
import urllib.request
import json
import io
import time
import os
import math
import hashlib
import threading
import webbrowser
import datetime
from contextlib import asynccontextmanager
from typing import Optional, List
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import chess
import chess.pgn
import chess.engine
from plants_app import plants_router, init_plant_db

# ================= CONFIGURATION =================
CHESSCOM_USER   = "Tmimer"
LICHESS_USER    = "DepressoCoffee"
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DB_PATH         = os.path.join(BASE_DIR, "chess_tactics.db")
STOCKFISH_EXE   = os.path.join(BASE_DIR, "stockfish.exe")

ENGINE_DEPTH        = 15
MIN_CP_DROP         = 150           # Minimum centipawn loss
MIN_WIN_PROB_DROP   = 0.15          # Minimum 15% win-probability loss
POLL_INTERVAL_SEC   = 600           # Poll every 10 minutes
CC_ARCHIVE_MONTHS   = 3            # How many monthly archives to fetch from Chess.com
LICHESS_MAX_GAMES   = 100          # Lichess games per poll

# Lichess puzzle seeder config
LICHESS_PUZZLE_MOTIFS = [
    "fork", "pin", "discoveredAttack", "hangingPiece",
    "backRankMate", "deflection"
]
LICHESS_PUZZLE_MIN_RATING = 1650
LICHESS_PUZZLE_MAX_RATING = 2150
LICHESS_PUZZLE_PER_MOTIF  = 50     # How many puzzles to seed per motif cycle

# Multi-move puzzle line builder
PUZZLE_MAX_PLY           = 9    # Max total half-moves to extend after the first move
FORCED_MOVE_THRESHOLD_CP = 100  # Stop extending when 2nd-best is within this margin (multiple wins)
# =================================================

daemon_status = {"state": "Starting...", "last_game": "None"}

# ---------- per-connection WAL helper ----------
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=20000;")
    conn.row_factory = sqlite3.Row
    return conn


# ===================== DATABASE INIT & MIGRATIONS =====================
def init_db():
    conn = get_db()
    cur = conn.cursor()

    # -- Core tables (idempotent) --
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS processed_games (
            game_id      TEXT PRIMARY KEY,
            platform     TEXT,
            white        TEXT,
            black        TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tactics (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id          TEXT,
            platform         TEXT,
            ply_number       INTEGER,
            fen_before       TEXT,
            turn             TEXT,
            category         TEXT,
            played_move_san  TEXT,
            best_move_san    TEXT,
            best_move_uci    TEXT,
            eval_before_cp   INTEGER,
            eval_after_cp    INTEGER,
            eval_drop_cp     INTEGER,
            motif            TEXT,
            solved_count     INTEGER DEFAULT 0,
            last_reviewed    TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS woodpecker_sets (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS woodpecker_set_puzzles (
            set_id    INTEGER REFERENCES woodpecker_sets(id),
            puzzle_id INTEGER REFERENCES tactics(id),
            position  INTEGER,
            PRIMARY KEY (set_id, puzzle_id)
        );

        CREATE TABLE IF NOT EXISTS woodpecker_progress (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            set_id          INTEGER REFERENCES woodpecker_sets(id),
            cycle_number    INTEGER DEFAULT 1,
            started_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at    TIMESTAMP,
            total_time_ms   INTEGER DEFAULT 0,
            correct_first   INTEGER DEFAULT 0,
            total_attempts  INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS woodpecker_puzzle_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            progress_id     INTEGER REFERENCES woodpecker_progress(id),
            puzzle_id       INTEGER REFERENCES tactics(id),
            correct_first   INTEGER,
            time_ms         INTEGER,
            answered_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # -- Safe ALTER TABLE migrations for SM-2 columns --
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(tactics)").fetchall()}
    migrations = [
        ("attempts",      "INTEGER DEFAULT 0"),
        ("ease_factor",   "REAL    DEFAULT 2.5"),
        ("interval_days", "REAL    DEFAULT 0"),
        ("repetitions",   "INTEGER DEFAULT 0"),
        ("next_review",   "TIMESTAMP"),
        ("is_seeded",     "INTEGER DEFAULT 0"),  # 1 = from Lichess puzzle DB
        ("puzzle_line",   "TEXT"),               # JSON list of UCI moves for multi-move line
    ]
    for col, col_def in migrations:
        if col not in existing_cols:
            cur.execute(f"ALTER TABLE tactics ADD COLUMN {col} {col_def}")

    conn.commit()
    conn.close()


# ===================== UTILITY FUNCTIONS =====================
def cp_to_win_prob(cp: int) -> float:
    clamped = max(-4000, min(4000, cp))
    return 1.0 / (1.0 + math.exp(-0.00368208 * clamped))


def extract_game_id(game: chess.pgn.Game, platform: str) -> str:
    link = game.headers.get("Link", "").strip()
    if link.startswith("http"):
        return link
    site = game.headers.get("Site", "").strip()
    if site.startswith("http"):
        return site
    raw_id = (
        f"{platform}_{game.headers.get('White')}_{game.headers.get('Black')}"
        f"_{game.headers.get('Date')}_{game.headers.get('UTCTime')}"
    )
    return hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:24]


def detect_motif(board: chess.Board, best_move: chess.Move, score_before: chess.engine.Score) -> str:
    board_after = board.copy()
    board_after.push(best_move)
    mover_color = board.turn
    enemy_color = not mover_color

    if board_after.is_checkmate():
        if chess.square_rank(best_move.to_square) in (0, 7):
            return "Back-Rank Mate"
        return "Checkmate"
    if score_before.is_mate() and score_before.mate() and score_before.mate() > 0:
        return f"Forced Mate in {score_before.mate()}"

    if board_after.is_check():
        checkers = board_after.checkers()
        if best_move.to_square not in checkers:
            return "Discovered Check"

    attacks_before = set()
    for sq in chess.SQUARES:
        p = board.piece_at(sq)
        if p and p.color == mover_color and p.piece_type in (chess.ROOK, chess.BISHOP, chess.QUEEN) \
                and sq != best_move.from_square:
            for att_sq in board.attacks(sq):
                ep = board.piece_at(att_sq)
                if ep and ep.color == enemy_color and ep.piece_type in (
                        chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT):
                    attacks_before.add((sq, att_sq))

    for sq in chess.SQUARES:
        p = board_after.piece_at(sq)
        if p and p.color == mover_color and p.piece_type in (chess.ROOK, chess.BISHOP, chess.QUEEN) \
                and sq != best_move.to_square:
            for att_sq in board_after.attacks(sq):
                ep = board_after.piece_at(att_sq)
                if ep and ep.color == enemy_color and ep.piece_type in (
                        chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT):
                    if (sq, att_sq) not in attacks_before:
                        return "Discovered Attack"

    high_value_targets = 0
    for sq in board_after.attacks(best_move.to_square):
        p = board_after.piece_at(sq)
        if p and p.color == enemy_color:
            if p.piece_type in (chess.KING, chess.QUEEN, chess.ROOK) \
                    or len(board_after.attackers(enemy_color, sq)) == 0:
                high_value_targets += 1
    if high_value_targets >= 2:
        return "Fork / Double Attack"

    for sq in chess.SQUARES:
        p = board_after.piece_at(sq)
        if p and p.color == enemy_color and p.piece_type != chess.PAWN:
            if board_after.is_pinned(enemy_color, sq) and not board.is_pinned(enemy_color, sq):
                return "Pin / Tactical Bind"

    if board.is_capture(best_move) and not board.is_en_passant(best_move):
        captured = board.piece_at(best_move.to_square)
        if captured and captured.piece_type != chess.PAWN:
            if len(board.attackers(enemy_color, best_move.to_square)) == 0:
                return "Hanging Piece Capture"
        return "Winning Capture / Trade"

    if board_after.is_check():
        return "Forcing Check"

    return "Positional / Prophylactic Resource"


# ===================== MULTI-MOVE PUZZLE LINE BUILDER =====================
def build_puzzle_line(
    board_before: chess.Board,
    first_move: chess.Move,
    engine: chess.engine.SimpleEngine,
) -> list:
    """
    Extend a blunder/tactic position into a full forced-move sequence.

    Returns a list of UCI strings alternating:
        [user_move_1, opp_reply_1, user_move_2, opp_reply_2, ...]

    Stops when:
      - The position is no longer "forced" for the user (2nd-best move is within
        FORCED_MOVE_THRESHOLD_CP of the best → multiple ways to win → stop here,
        the puzzle is solved)
      - The game is over (checkmate / stalemate)
      - PUZZLE_MAX_PLY total half-moves have been recorded
    """
    line: list = [first_move.uci()]
    board = board_before.copy()
    board.push(first_move)

    for step in range(PUZZLE_MAX_PLY - 1):
        if board.is_game_over():
            break

        is_opp_turn = (step % 2 == 0)   # step 0 → opponent replies to first_move

        try:
            if is_opp_turn:
                # Play the engine's single best reply for the opponent
                info = engine.analyse(board, chess.engine.Limit(depth=ENGINE_DEPTH))
                pv = info.get("pv", [])
                if not pv:
                    break
                opp_move = pv[0]
                line.append(opp_move.uci())
                board.push(opp_move)
            else:
                # User's continuation — check whether there is a *single* forced winning move
                infos = engine.analyse(
                    board, chess.engine.Limit(depth=ENGINE_DEPTH), multipv=2
                )
                if not infos or not infos[0].get("pv"):
                    break

                best_move = infos[0]["pv"][0]
                s1 = infos[0]["score"].pov(board.turn)

                if len(infos) >= 2:
                    s2 = infos[1]["score"].pov(board.turn)
                    # For non-mate positions check centipawn gap
                    if not s1.is_mate():
                        cp1 = s1.score(mate_score=10000)
                        cp2 = s2.score(mate_score=10000)
                        if cp1 is not None and cp2 is not None:
                            if cp1 - cp2 < FORCED_MOVE_THRESHOLD_CP:
                                # Multiple moves win → puzzle is "complete" here
                                break
                    # For forced-mate positions keep going until checkmate

                line.append(best_move.uci())
                board.push(best_move)

        except Exception as e_inner:
            print(f"    [!] build_puzzle_line step {step}: {e_inner}")
            break

    return line


# ===================== SM-2 ALGORITHM =====================
def sm2_update(ease_factor: float, interval_days: float, repetitions: int, correct_first_try: bool):
    """
    Standard SM-2 algorithm.
    Returns (new_ease_factor, new_interval_days, new_repetitions, next_review_dt)
    """
    if not correct_first_try:
        # Failed: reset streak, review again tomorrow
        new_repetitions = 0
        new_interval    = 1.0
        new_ef          = max(1.3, ease_factor - 0.2)
    else:
        if repetitions == 0:
            new_interval = 1.0
        elif repetitions == 1:
            new_interval = 6.0
        else:
            new_interval = round(interval_days * ease_factor, 1)
        new_repetitions = repetitions + 1
        # EF adjustment for correct response (grade = 4 for first-try correct)
        new_ef = ease_factor + (0.1 - (5 - 4) * (0.08 + (5 - 4) * 0.02))
        new_ef = max(1.3, new_ef)

    next_review = datetime.datetime.utcnow() + datetime.timedelta(days=new_interval)
    return new_ef, new_interval, new_repetitions, next_review.strftime("%Y-%m-%d %H:%M:%S")


# ===================== GAME ANALYSIS =====================
def analyze_pgn_game(game: chess.pgn.Game, target_user: str, platform: str,
                     engine: chess.engine.SimpleEngine):
    game_id = extract_game_id(game, platform)
    conn = get_db()
    cur  = conn.cursor()

    cur.execute("SELECT 1 FROM processed_games WHERE game_id = ?", (game_id,))
    if cur.fetchone():
        conn.close()
        return

    white_name = game.headers.get("White", "")
    black_name = game.headers.get("Black", "")
    user_lower = target_user.lower()

    if white_name.lower() != user_lower and black_name.lower() != user_lower:
        conn.close()
        return

    user_color = chess.WHITE if white_name.lower() == user_lower else chess.BLACK
    board  = game.board()
    moves  = list(game.mainline_moves())

    if len(moves) < 8:
        cur.execute(
            "INSERT OR IGNORE INTO processed_games (game_id, platform, white, black) VALUES (?, ?, ?, ?)",
            (game_id, platform, white_name, black_name),
        )
        conn.commit()
        conn.close()
        return

    daemon_status["state"]     = f"Analyzing ({platform})"
    daemon_status["last_game"] = f"{white_name} vs {black_name}"
    print(f"[+] Analyzing {platform}: {white_name} vs {black_name} ({game_id})")

    prev_info = engine.analyse(board, chess.engine.Limit(depth=ENGINE_DEPTH))
    opponent_blundered_prev_ply = False

    for ply, move in enumerate(moves):
        fen_before  = board.fen()
        turn_color  = board.turn
        is_user_turn = (turn_color == user_color)

        best_move       = prev_info.get("pv", [None])[0]
        score_before_pov = prev_info["score"].pov(turn_color)
        cp_before        = score_before_pov.score(mate_score=10000)

        board.push(move)
        curr_info      = engine.analyse(board, chess.engine.Limit(depth=ENGINE_DEPTH))
        score_after_pov = -curr_info["score"].pov(board.turn)
        cp_after        = score_after_pov.score(mate_score=10000)

        if best_move and move != best_move and cp_before is not None and cp_after is not None:
            cp_drop          = cp_before - cp_after
            win_prob_before  = cp_to_win_prob(cp_before)
            win_prob_after   = cp_to_win_prob(cp_after)
            prob_drop        = win_prob_before - win_prob_after

            if cp_drop >= MIN_CP_DROP and prob_drop >= MIN_WIN_PROB_DROP:
                board_before = chess.Board(fen_before)
                motif        = detect_motif(board_before, best_move, score_before_pov)

                if is_user_turn:
                    if opponent_blundered_prev_ply and cp_before >= 150:
                        category = "My Missed Punishment"
                    elif cp_before >= 200 and cp_after < 100:
                        category = "My Missed Win"
                    else:
                        category = "My Blunder"
                else:
                    if cp_before >= 180 and prob_drop >= 0.18:
                        category = "Opponent Missed Win"
                    else:
                        category = None

                if category:
                    # Build multi-move forced line from the blunder position
                    try:
                        puzzle_line = build_puzzle_line(board_before, best_move, engine)
                    except Exception as e_line:
                        print(f"    [!] Line build failed: {e_line}")
                        puzzle_line = [best_move.uci()]

                    cur.execute("""
                        INSERT INTO tactics (
                            game_id, platform, ply_number, fen_before, turn, category,
                            played_move_san, best_move_san, best_move_uci,
                            eval_before_cp, eval_after_cp, eval_drop_cp, motif, puzzle_line
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        game_id, platform, ply + 1, fen_before,
                        "White" if turn_color == chess.WHITE else "Black",
                        category,
                        board_before.san(move),
                        board_before.san(best_move),
                        best_move.uci(),
                        cp_before, cp_after, cp_drop, motif,
                        json.dumps(puzzle_line),
                    ))
                    conn.commit()
                    line_len = len(puzzle_line)
                    moves_str = f"{(line_len + 1) // 2}-move line" if line_len > 1 else "1-move"
                    print(f"    -> [{category}] Ply {ply+1}: Played {board_before.san(move)}, "
                          f"Best {board_before.san(best_move)} (-{cp_drop}cp | {motif}) [{moves_str}]")

                opponent_blundered_prev_ply = not is_user_turn
            else:
                opponent_blundered_prev_ply = False
        else:
            opponent_blundered_prev_ply = False

        prev_info = curr_info

    cur.execute(
        "INSERT OR IGNORE INTO processed_games (game_id, platform, white, black) VALUES (?, ?, ?, ?)",
        (game_id, platform, white_name, black_name),
    )
    conn.commit()
    conn.close()


# ===================== CHESS.COM FETCHER (last N monthly archives) =====================
def fetch_chesscom_games(engine: chess.engine.SimpleEngine):
    """Fetch last CC_ARCHIVE_MONTHS monthly archives and analyse each game."""
    daemon_status["state"] = f"Fetching Chess.com archives ({CHESSCOM_USER})..."
    try:
        arch_url = f"https://api.chess.com/pub/player/{CHESSCOM_USER}/games/archives"
        req = urllib.request.Request(
            arch_url,
            headers={"User-Agent": f"PersonalTacticsTrainer/2.0 ({CHESSCOM_USER})"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            archive_list = json.loads(resp.read().decode("utf-8"))["archives"]
    except Exception as e:
        print(f"[!] Chess.com archives list error: {e}")
        return

    # Most-recent N months last in the list
    recent_archives = archive_list[-CC_ARCHIVE_MONTHS:]
    for arch_url_pgn in reversed(recent_archives):
        pgn_url = arch_url_pgn + "/pgn"
        try:
            req = urllib.request.Request(
                pgn_url,
                headers={"User-Agent": f"PersonalTacticsTrainer/2.0 ({CHESSCOM_USER})"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                pgn_text = resp.read().decode("utf-8", errors="replace")
            pgn_io = io.StringIO(pgn_text)
            games  = []
            while True:
                g = chess.pgn.read_game(pgn_io)
                if not g:
                    break
                games.append(g)
            print(f"[+] Chess.com archive {pgn_url}: {len(games)} games")
            for g in reversed(games):          # oldest → newest within month
                analyze_pgn_game(g, CHESSCOM_USER, "Chess.com", engine)
        except Exception as e:
            print(f"[!] Chess.com fetch error ({pgn_url}): {e}")


# ===================== LICHESS FETCHER =====================
def fetch_lichess_games(engine: chess.engine.SimpleEngine):
    """Fetch LICHESS_MAX_GAMES most recent games."""
    daemon_status["state"] = f"Fetching Lichess ({LICHESS_USER})..."
    li_url = (
        f"https://lichess.org/api/games/user/{LICHESS_USER}"
        f"?max={LICHESS_MAX_GAMES}&pgnInJson=true"
    )
    req_li = urllib.request.Request(
        li_url,
        headers={
            "User-Agent": f"PersonalTacticsTrainer/2.0 ({LICHESS_USER})",
            "Accept":     "application/x-ndjson",
        },
    )
    try:
        with urllib.request.urlopen(req_li, timeout=30) as resp:
            for line in resp:
                if line.strip():
                    data     = json.loads(line.decode("utf-8", errors="replace"))
                    pgn_text = data.get("pgn", "")
                    if pgn_text:
                        g = chess.pgn.read_game(io.StringIO(pgn_text))
                        if g:
                            analyze_pgn_game(g, LICHESS_USER, "Lichess", engine)
    except Exception as e:
        print(f"[!] Lichess fetch error: {e}")


# ===================== LICHESS PUZZLE SEEDER =====================
def seed_lichess_puzzles():
    """
    Pull open-database puzzles from Lichess matching the user's most-missed motifs.
    Maps our internal motif names → Lichess puzzle theme names.
    """
    MOTIF_MAP = {
        "Fork / Double Attack":          "fork",
        "Pin / Tactical Bind":           "pin",
        "Discovered Attack":             "discoveredAttack",
        "Hanging Piece Capture":         "hangingPiece",
        "Back-Rank Mate":                "backRankMate",
        "Positional / Prophylactic Resource": "deflection",
    }

    # Find the top missed motifs (unsolved personal blunders)
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT motif, COUNT(*) as cnt
        FROM tactics
        WHERE is_seeded = 0
          AND (solved_count IS NULL OR solved_count = 0)
          AND motif IS NOT NULL
        GROUP BY motif
        ORDER BY cnt DESC
        LIMIT 6
    """)
    missed_motifs = [row[0] for row in cur.fetchall()]
    conn.close()

    # Fall back to full list if nothing missed yet
    if not missed_motifs:
        missed_motifs = list(MOTIF_MAP.keys())

    for motif in missed_motifs:
        lichess_theme = MOTIF_MAP.get(motif)
        if not lichess_theme:
            continue

        url = (
            f"https://lichess.org/api/puzzle/next"
            f"?themes={lichess_theme}"
            f"&ratingMin={LICHESS_PUZZLE_MIN_RATING}"
            f"&ratingMax={LICHESS_PUZZLE_MAX_RATING}"
        )
        # Lichess puzzle API returns one puzzle per call; batch by LICHESS_PUZZLE_PER_MOTIF
        conn = get_db()
        cur  = conn.cursor()
        inserted = 0
        for _ in range(LICHESS_PUZZLE_PER_MOTIF):
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": f"PersonalTacticsTrainer/2.0"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                print(f"[!] Lichess puzzle seeder error ({lichess_theme}): {e}")
                break

            puzzle = data.get("puzzle", {})
            game   = data.get("game", {})
            fen_before = puzzle.get("initialFen") or game.get("pgn", "")

            # The puzzle may give a FEN directly or embed it in pgn
            if not fen_before:
                continue

            # Extract FEN from pgn if needed (Lichess puzzle API gives FEN directly)
            solution_moves = puzzle.get("solution", [])
            if not solution_moves:
                continue

            best_move_uci = solution_moves[0]
            puzzle_id_ext = puzzle.get("id", "")
            game_url      = f"https://lichess.org/training/{puzzle_id_ext}"

            # Convert UCI to SAN
            try:
                b = chess.Board(fen_before)
                m = chess.Move.from_uci(best_move_uci)
                best_move_san = b.san(m)
                turn_str      = "White" if b.turn == chess.WHITE else "Black"
            except Exception:
                continue

            # Deduplicate by game_url (puzzle id)
            cur.execute("SELECT id FROM tactics WHERE game_id = ?", (game_url,))
            if cur.fetchone():
                continue

            cur.execute("""
                INSERT INTO tactics (
                    game_id, platform, ply_number, fen_before, turn, category,
                    played_move_san, best_move_san, best_move_uci,
                    eval_before_cp, eval_after_cp, eval_drop_cp, motif, is_seeded, puzzle_line
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                game_url, "Lichess-Puzzles", 0, fen_before, turn_str,
                "Lichess Training",
                "—", best_move_san, best_move_uci,
                0, 0, 0, motif, 1,
                json.dumps(solution_moves),   # Lichess provides the full solution line
            ))
            conn.commit()
            inserted += 1
            time.sleep(0.12)   # polite rate-limiting

        conn.close()
        if inserted:
            print(f"[+] Seeded {inserted} Lichess puzzles for motif '{lichess_theme}'")


# ===================== BACKGROUND DAEMON =====================
def background_daemon():
    init_db()
    if not os.path.exists(STOCKFISH_EXE):
        daemon_status["state"] = "ERROR: stockfish.exe missing in folder!"
        print(f"[!] CRITICAL: {STOCKFISH_EXE} not found. Place stockfish.exe in {BASE_DIR}")
        return

    print(f"[+] Starting Stockfish engine: {STOCKFISH_EXE}")
    engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_EXE)

    cycle = 0
    while True:
        try:
            fetch_chesscom_games(engine)
            fetch_lichess_games(engine)

            # Seed Lichess motif puzzles every 3rd cycle (not every 10 min)
            if cycle % 3 == 0:
                seed_thread = threading.Thread(target=seed_lichess_puzzles, daemon=True)
                seed_thread.start()

            daemon_status["state"] = "Idle (Waiting for next poll)"
            cycle += 1
        except Exception as e:
            daemon_status["state"] = f"Error: {e}"
            print(f"[!] Daemon loop error: {e}")

        time.sleep(POLL_INTERVAL_SEC)


# ===================== FASTAPI APP =====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_plant_db()
    worker = threading.Thread(target=background_daemon, daemon=True)
    worker.start()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include modular Plant Watering Tracker router
app.include_router(plants_router)


# ---------- Serve UI ----------
@app.get("/")
@app.get("/chess")
def serve_ui():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


# ---------- Stats ----------
@app.get("/api/stats")
def get_stats():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM processed_games")
    games_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM tactics")
    tactics_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM tactics WHERE solved_count = 0")
    unsolved_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM tactics WHERE next_review <= CURRENT_TIMESTAMP OR next_review IS NULL")
    due_count = cur.fetchone()[0]
    conn.close()
    return {
        "games_analyzed":  games_count,
        "total_puzzles":   tactics_count,
        "unsolved_puzzles": unsolved_count,
        "due_puzzles":      due_count,
        "daemon_state":    daemon_status["state"],
        "last_game":       daemon_status["last_game"],
    }


# ---------- Next Puzzle (SM-2 priority queue) ----------
@app.get("/api/puzzle/next")
def get_next_puzzle(
    category: Optional[str] = Query(default="ALL"),
    skip_id:  Optional[int] = Query(default=0),
    mode:     Optional[str] = Query(default="sr"),  # "sr" | "woodpecker"
):
    conn = get_db()
    cur  = conn.cursor()

    base_select = """
        SELECT id, game_id, platform, ply_number, fen_before, turn, category,
               played_move_san, best_move_san, best_move_uci, eval_drop_cp, motif,
               solved_count, attempts, ease_factor, interval_days, repetitions, next_review,
               puzzle_line
        FROM tactics
        WHERE id != ?
    """
    params = [skip_id]

    if category and category != "ALL":
        base_select += " AND category = ?"
        params.append(category)

    if mode == "sr":
        # Prioritise due puzzles (next_review <= now or null), then by solved_count asc
        query = base_select + """
            ORDER BY
                CASE WHEN next_review IS NULL OR next_review <= CURRENT_TIMESTAMP THEN 0 ELSE 1 END ASC,
                solved_count ASC,
                id DESC
            LIMIT 1
        """
    else:
        query = base_select + " ORDER BY solved_count ASC, id DESC LIMIT 1"

    cur.execute(query, params)
    row = cur.fetchone()
    conn.close()

    if not row:
        return {"has_puzzles": False}

    pl_raw = row["puzzle_line"]
    puzzle_line = json.loads(pl_raw) if pl_raw else [row["best_move_uci"]]

    return {
        "has_puzzles":     True,
        "id":              row["id"],
        "game_url":        row["game_id"],
        "platform":        row["platform"],
        "ply":             row["ply_number"],
        "fen":             row["fen_before"],
        "turn":            row["turn"],
        "category":        row["category"],
        "played_move_san": row["played_move_san"],
        "best_move_san":   row["best_move_san"],
        "best_move_uci":   row["best_move_uci"],
        "eval_drop":       row["eval_drop_cp"],
        "motif":           row["motif"],
        "solved_count":    row["solved_count"],
        "attempts":        row["attempts"],
        "ease_factor":     row["ease_factor"],
        "interval_days":   row["interval_days"],
        "repetitions":     row["repetitions"],
        "next_review":     row["next_review"],
        "puzzle_line":     puzzle_line,
    }


# ---------- SM-2 Attempt ----------
class AttemptPayload(BaseModel):
    correct_first_try: bool
    time_ms:           int = 0


@app.post("/api/puzzle/{puzzle_id}/attempt")
def record_attempt(puzzle_id: int, payload: AttemptPayload):
    conn = get_db()
    cur  = conn.cursor()

    cur.execute(
        "SELECT ease_factor, interval_days, repetitions FROM tactics WHERE id = ?",
        (puzzle_id,)
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Puzzle not found")

    ef, interval, reps = row["ease_factor"], row["interval_days"], row["repetitions"]
    if ef is None:   ef       = 2.5
    if interval is None: interval = 0.0
    if reps is None: reps     = 0

    new_ef, new_interval, new_reps, next_review = sm2_update(
        ef, interval, reps, payload.correct_first_try
    )

    cur.execute("""
        UPDATE tactics
        SET attempts      = COALESCE(attempts, 0) + 1,
            solved_count  = solved_count + ?,
            ease_factor   = ?,
            interval_days = ?,
            repetitions   = ?,
            next_review   = ?,
            last_reviewed = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (
        1 if payload.correct_first_try else 0,
        new_ef, new_interval, new_reps, next_review,
        puzzle_id,
    ))
    conn.commit()
    conn.close()
    return {
        "status":       "ok",
        "ease_factor":  new_ef,
        "interval_days": new_interval,
        "next_review":  next_review,
    }


# ---------- Legacy solve endpoint (keep existing clients working) ----------
@app.post("/api/puzzle/{puzzle_id}/solve")
def mark_solved(puzzle_id: int):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE tactics
        SET solved_count  = solved_count + 1,
            last_reviewed = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (puzzle_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


# ===================== WOODPECKER ENDPOINTS =====================

class WoodpeckerCreatePayload(BaseModel):
    name:     str         = "My Woodpecker Set"
    size:     int         = 200
    category: Optional[str] = None


@app.post("/api/woodpecker/create")
def create_woodpecker_set(payload: WoodpeckerCreatePayload):
    """Generate a fixed set of N puzzles blending personal blunders + seeded motif puzzles."""
    conn = get_db()
    cur  = conn.cursor()

    size = max(10, min(payload.size, 1000))

    where_cat = ""
    params: list = []
    if payload.category and payload.category != "ALL":
        where_cat = "AND category = ?"
        params.append(payload.category)

    # Prefer personal blunders (is_seeded=0), pad with seeded if needed
    cur.execute(f"""
        SELECT id FROM tactics
        WHERE is_seeded = 0 {where_cat}
        ORDER BY RANDOM()
        LIMIT ?
    """, params + [size])
    personal_ids = [row[0] for row in cur.fetchall()]

    remaining = size - len(personal_ids)
    seeded_ids: list = []
    if remaining > 0:
        cur.execute(f"""
            SELECT id FROM tactics
            WHERE is_seeded = 1 {where_cat}
            ORDER BY RANDOM()
            LIMIT ?
        """, params + [remaining])
        seeded_ids = [row[0] for row in cur.fetchall()]

    all_ids = personal_ids + seeded_ids
    if not all_ids:
        conn.close()
        return {"error": "No puzzles available. Let the daemon run first."}

    cur.execute(
        "INSERT INTO woodpecker_sets (name) VALUES (?)", (payload.name,)
    )
    set_id = cur.lastrowid

    cur.executemany(
        "INSERT INTO woodpecker_set_puzzles (set_id, puzzle_id, position) VALUES (?, ?, ?)",
        [(set_id, pid, pos) for pos, pid in enumerate(all_ids)]
    )

    # Create the first cycle progress row
    cur.execute(
        "INSERT INTO woodpecker_progress (set_id, cycle_number) VALUES (?, 1)",
        (set_id,)
    )
    progress_id = cur.lastrowid

    conn.commit()
    conn.close()
    return {"set_id": set_id, "progress_id": progress_id, "puzzle_count": len(all_ids)}


@app.get("/api/woodpecker/{set_id}/status")
def woodpecker_status(set_id: int):
    conn = get_db()
    cur  = conn.cursor()

    cur.execute("SELECT * FROM woodpecker_sets WHERE id = ?", (set_id,))
    ws = cur.fetchone()
    if not ws:
        conn.close()
        raise HTTPException(status_code=404, detail="Set not found")

    cur.execute(
        "SELECT * FROM woodpecker_progress WHERE set_id = ? ORDER BY id DESC LIMIT 1",
        (set_id,)
    )
    prog = cur.fetchone()

    cur.execute(
        "SELECT COUNT(*) FROM woodpecker_set_puzzles WHERE set_id = ?", (set_id,)
    )
    total = cur.fetchone()[0]

    answered = 0
    correct_first = 0
    if prog:
        cur.execute("""
            SELECT COUNT(*), SUM(correct_first)
            FROM woodpecker_puzzle_log
            WHERE progress_id = ?
        """, (prog["id"],))
        row = cur.fetchone()
        answered      = row[0] or 0
        correct_first = int(row[1] or 0)

    conn.close()
    return {
        "set_id":         set_id,
        "name":           ws["name"],
        "cycle_number":   prog["cycle_number"] if prog else 1,
        "total_puzzles":  total,
        "answered":       answered,
        "remaining":      max(0, total - answered),
        "correct_first":  correct_first,
        "accuracy_pct":   round(correct_first / answered * 100, 1) if answered > 0 else 0.0,
        "total_time_ms":  prog["total_time_ms"] if prog else 0,
        "completed_at":   prog["completed_at"] if prog else None,
    }


@app.get("/api/woodpecker/{set_id}/next")
def woodpecker_next_puzzle(set_id: int):
    """Return the next unanswered puzzle in the current cycle."""
    conn = get_db()
    cur  = conn.cursor()

    cur.execute(
        "SELECT id FROM woodpecker_progress WHERE set_id = ? ORDER BY id DESC LIMIT 1",
        (set_id,)
    )
    prog_row = cur.fetchone()
    if not prog_row:
        conn.close()
        raise HTTPException(status_code=404, detail="No active cycle")

    progress_id = prog_row["id"]

    # Get answered puzzle ids in this cycle
    cur.execute(
        "SELECT puzzle_id FROM woodpecker_puzzle_log WHERE progress_id = ?",
        (progress_id,)
    )
    answered_ids = {row[0] for row in cur.fetchall()}

    cur.execute("""
        SELECT wsp.puzzle_id, t.fen_before, t.turn, t.category, t.motif,
               t.best_move_san, t.best_move_uci, t.eval_drop_cp, t.game_id,
               t.platform, t.ply_number, t.played_move_san, t.puzzle_line
        FROM woodpecker_set_puzzles wsp
        JOIN tactics t ON t.id = wsp.puzzle_id
        WHERE wsp.set_id = ?
        ORDER BY wsp.position ASC
    """, (set_id,))
    rows = cur.fetchall()
    conn.close()

    for row in rows:
        if row["puzzle_id"] not in answered_ids:
            pl_raw = row["puzzle_line"]
            puzzle_line = json.loads(pl_raw) if pl_raw else [row["best_move_uci"]]
            return {
                "has_puzzles":     True,
                "id":              row["puzzle_id"],
                "game_url":        row["game_id"],
                "platform":        row["platform"],
                "ply":             row["ply_number"],
                "fen":             row["fen_before"],
                "turn":            row["turn"],
                "category":        row["category"],
                "played_move_san": row["played_move_san"],
                "best_move_san":   row["best_move_san"],
                "best_move_uci":   row["best_move_uci"],
                "eval_drop":       row["eval_drop_cp"],
                "motif":           row["motif"],
                "puzzle_line":     puzzle_line,
            }

    return {"has_puzzles": False, "cycle_complete": True}


class WoodpeckerAttemptPayload(BaseModel):
    set_id:            int
    correct_first_try: bool
    time_ms:           int = 0


@app.post("/api/woodpecker/attempt")
def woodpecker_attempt(payload: WoodpeckerAttemptPayload):
    """Record a puzzle attempt in the current Woodpecker cycle."""
    conn = get_db()
    cur  = conn.cursor()

    cur.execute(
        "SELECT id FROM woodpecker_progress WHERE set_id = ? ORDER BY id DESC LIMIT 1",
        (payload.set_id,)
    )
    prog = cur.fetchone()
    if not prog:
        conn.close()
        raise HTTPException(status_code=404, detail="No active cycle")

    progress_id = prog["id"]

    # Find the puzzle being answered (first unanswered in order)
    cur.execute(
        "SELECT puzzle_id FROM woodpecker_puzzle_log WHERE progress_id = ?",
        (progress_id,)
    )
    answered_ids = {row[0] for row in cur.fetchall()}

    cur.execute("""
        SELECT wsp.puzzle_id
        FROM woodpecker_set_puzzles wsp
        WHERE wsp.set_id = ?
        ORDER BY wsp.position ASC
    """, (payload.set_id,))
    all_rows = cur.fetchall()

    next_puzzle_id = None
    for row in all_rows:
        if row[0] not in answered_ids:
            next_puzzle_id = row[0]
            break

    if next_puzzle_id is None:
        conn.close()
        return {"error": "No unanswered puzzle found"}

    cur.execute("""
        INSERT INTO woodpecker_puzzle_log (progress_id, puzzle_id, correct_first, time_ms)
        VALUES (?, ?, ?, ?)
    """, (progress_id, next_puzzle_id, 1 if payload.correct_first_try else 0, payload.time_ms))

    cur.execute("""
        UPDATE woodpecker_progress
        SET total_time_ms   = total_time_ms + ?,
            total_attempts  = total_attempts + 1,
            correct_first   = correct_first + ?
        WHERE id = ?
    """, (payload.time_ms, 1 if payload.correct_first_try else 0, progress_id))

    # Check if all puzzles answered → complete cycle
    total_answered = len(answered_ids) + 1
    total_puzzles  = len(all_rows)
    cycle_done     = (total_answered >= total_puzzles)

    if cycle_done:
        cur.execute("""
            UPDATE woodpecker_progress
            SET completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (progress_id,))
        # Start next cycle
        cur.execute(
            "SELECT cycle_number, total_time_ms, correct_first FROM woodpecker_progress WHERE id = ?",
            (progress_id,)
        )
        last = cur.fetchone()
        cur.execute("""
            INSERT INTO woodpecker_progress (set_id, cycle_number)
            VALUES (?, ?)
        """, (payload.set_id, (last["cycle_number"] or 1) + 1))

    conn.commit()
    conn.close()
    return {"status": "ok", "cycle_complete": cycle_done}


@app.get("/api/woodpecker/list")
def list_woodpecker_sets():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT ws.id, ws.name, ws.created_at,
               (SELECT COUNT(*) FROM woodpecker_set_puzzles wsp WHERE wsp.set_id = ws.id) AS puzzle_count,
               (SELECT cycle_number FROM woodpecker_progress wp WHERE wp.set_id = ws.id ORDER BY wp.id DESC LIMIT 1) AS cycle
        FROM woodpecker_sets ws
        ORDER BY ws.id DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id":           r["id"],
            "name":         r["name"],
            "created_at":   r["created_at"],
            "puzzle_count": r["puzzle_count"],
            "cycle":        r["cycle"] or 1,
        }
        for r in rows
    ]


# ===================== LICHESS PUZZLE SEEDER TRIGGER =====================
@app.post("/api/seed/lichess")
def trigger_lichess_seed():
    """Manually trigger a Lichess motif puzzle seeding pass."""
    t = threading.Thread(target=seed_lichess_puzzles, daemon=True)
    t.start()
    return {"status": "seeding started"}


if __name__ == "__main__":
    import uvicorn
    import socket

    def get_lan_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect(('10.255.255.255', 1))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                return "127.0.0.1"

    lan_ip = get_lan_ip()
    print("\n" + "=" * 70)
    print("🌱 Apartment Modular Home Server Starting...")
    print(f"🏠 Local Access:")
    print(f"   ♟️  Chess Trainer:   http://127.0.0.1:8000/chess  (or http://127.0.0.1:8000)")
    print(f"   🌿 Plant Tracker:   http://127.0.0.1:8000/plants")
    print(f"")
    print(f"📱 LAN / Mobile Wi-Fi Access (Open directly on your phone):")
    print(f"   ♟️  Chess Trainer:   http://{lan_ip}:8000/chess")
    print(f"   🌿 Plant Tracker:   http://{lan_ip}:8000/plants")
    print("=" * 70 + "\n")

    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:8000")).start()
    uvicorn.run(app, host="0.0.0.0", port=8000)
