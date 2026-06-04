from copy import deepcopy
import csv
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from urllib.parse import quote_plus
from uuid import uuid4
import xml.etree.ElementTree as ET

from flask import Flask, jsonify, redirect, render_template, request, url_for

try:
    import certifi
except ImportError:
    certifi = None

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import pymongo
    from pymongo import ASCENDING, DESCENDING, MongoClient, UpdateOne
    from pymongo.errors import OperationFailure, PyMongoError
except ImportError:
    pymongo = None
    ASCENDING = DESCENDING = MongoClient = UpdateOne = None
    OperationFailure = PyMongoError = Exception

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    import pytesseract
except ImportError:
    pytesseract = None

ENV_PATH = Path(__file__).with_name(".env")
DB_PATH = Path(__file__).with_name("cricket_stats.db")
IDEA_DATA_SOURCES_PATH = Path(__file__).with_name(".idea").joinpath("dataSources.xml")
IDEA_DATA_SOURCES_LOCAL_PATH = Path(__file__).with_name(".idea").joinpath("dataSources.local.xml")


def load_local_env(env_path):
    if not env_path.exists():
        return
    if load_dotenv is not None:
        load_dotenv(env_path, override=False)
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def read_int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def read_bool_env(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def find_pycharm_mongo_datasource():
    if not IDEA_DATA_SOURCES_PATH.exists() or not IDEA_DATA_SOURCES_LOCAL_PATH.exists():
        return None
    try:
        shared_tree = ET.parse(IDEA_DATA_SOURCES_PATH)
        local_tree = ET.parse(IDEA_DATA_SOURCES_LOCAL_PATH)
    except ET.ParseError:
        return None

    for shared_source in shared_tree.findall(".//data-source"):
        jdbc_url = (shared_source.findtext("jdbc-url") or "").strip()
        if not jdbc_url.startswith("mongodb"):
            continue
        source_uuid = shared_source.get("uuid")
        if not source_uuid:
            continue
        local_source = local_tree.find(f".//data-source[@uuid='{source_uuid}']")
        if local_source is None:
            continue
        username = (local_source.findtext("user-name") or "").strip()
        if not username:
            continue
        return {"jdbc_url": jdbc_url, "username": username}
    return None


def mongo_uri_from_pycharm_datasource():
    datasource = find_pycharm_mongo_datasource()
    if not datasource:
        return None, None
    password = (os.environ.get("MONGODB_PASSWORD") or "").strip()
    if not password:
        return None, "PyCharm MongoDB datasource found, but MONGODB_PASSWORD is missing in .env."

    jdbc_url = datasource["jdbc_url"]
    scheme, remainder = jdbc_url.split("://", 1)
    username = quote_plus(datasource["username"])
    encoded_password = quote_plus(password)
    return f"{scheme}://{username}:{encoded_password}@{remainder}", None


load_local_env(ENV_PATH)

derived_mongo_uri, derived_mongo_error = mongo_uri_from_pycharm_datasource()

app = Flask(__name__)
MONGO_URI = (os.environ.get("MONGODB_URI") or derived_mongo_uri or "").strip()
MONGO_DB_NAME = (os.environ.get("MONGODB_DB") or "cricket_scorer").strip() or "cricket_scorer"
MONGO_TIMEOUT_MS = read_int_env("MONGODB_TIMEOUT_MS", 10000)
MONGO_TLS_CA_FILE = (os.environ.get("MONGODB_TLS_CA_FILE") or "").strip()
MONGO_TLS_ALLOW_INVALID_CERTS = read_bool_env("MONGODB_TLS_ALLOW_INVALID_CERTS", False)
mongo_client = None
mongo_db = None
players_collection = None
history_collection = None
mongo_error = None


def mongo_runtime_issue():
    if derived_mongo_error and not MONGO_URI:
        return derived_mongo_error
    if not MONGO_URI:
        return None
    if pymongo is None or MongoClient is None:
        return "PyMongo is not installed. Install the packages in requirements.txt before enabling MongoDB."
    pymongo_version = getattr(pymongo, "version_tuple", (0,))
    if sys.version_info >= (3, 14) and pymongo_version[0] < 4:
        return (
            f"PyMongo {pymongo.version} is too old for Python {sys.version_info.major}.{sys.version_info.minor}. "
            "Use PyMongo 4.x for MongoDB Atlas on this runtime."
        )
    return None


def build_mongo_client(uri):
    client_kwargs = {
        "serverSelectionTimeoutMS": MONGO_TIMEOUT_MS,
        "connectTimeoutMS": MONGO_TIMEOUT_MS,
        "socketTimeoutMS": MONGO_TIMEOUT_MS,
    }
    if uri.startswith("mongodb+srv://"):
        client_kwargs["tls"] = True
    ca_file = MONGO_TLS_CA_FILE or (certifi.where() if certifi else "")
    if ca_file:
        client_kwargs["tlsCAFile"] = ca_file
    if MONGO_TLS_ALLOW_INVALID_CERTS:
        client_kwargs["tlsAllowInvalidCertificates"] = True
    return MongoClient(uri, **client_kwargs)


if MONGO_URI:
    mongo_error = mongo_runtime_issue()
    if not mongo_error:
        try:
            mongo_client = build_mongo_client(MONGO_URI)
            mongo_db = mongo_client[MONGO_DB_NAME]
            players_collection = mongo_db["player_stats"]
            history_collection = mongo_db["match_history"]
        except Exception as error:
            mongo_error = str(error)


DEFAULT_PLAYERS = []
DEFAULT_OPPONENT_PLAYERS = []
PLAYER_IMPORT_FIELDS = [
    "matches",
    "batting_innings",
    "not_outs",
    "runs",
    "balls",
    "fours",
    "sixes",
    "ducks",
    "high_score",
    "bowling_innings",
    "balls_bowled",
    "runs_conceded",
    "wickets",
    "maidens",
    "best_wickets",
    "best_runs",
]
PLAYER_IMPORT_HEADER_ALIASES = {
    "#": "serial",
    "s_no": "serial",
    "sr_no": "serial",
    "serial_no": "serial",
    "sl_no": "serial",
    "rank": "serial",
    "mat": "matches",
    "player": "name",
    "player_name": "name",
    "playername": "name",
    "batter": "name",
    "batters": "name",
    "batsman": "name",
    "batsmen": "name",
    "bowler": "name",
    "bowlers": "name",
    "matches_played": "matches",
    "match_played": "matches",
    "matches": "matches",
    "batting_inns": "batting_innings",
    "batting_inning": "batting_innings",
    "bat_inns": "batting_innings",
    "bat_inning": "batting_innings",
    "innings_batted": "batting_innings",
    "inns": "batting_innings",
    "not_out": "not_outs",
    "notout": "not_outs",
    "n_o": "not_outs",
    "no": "not_outs",
    "run": "runs",
    "r": "runs",
    "runs_scored": "runs",
    "ball": "balls",
    "b": "balls",
    "bf": "balls",
    "balls_faced": "balls",
    "four": "fours",
    "4s": "fours",
    "six": "sixes",
    "6s": "sixes",
    "0": "ducks",
    "duck": "ducks",
    "hs": "high_score",
    "highest_score": "high_score",
    "highest": "high_score",
    "bowling_inns": "bowling_innings",
    "bowling_inning": "bowling_innings",
    "bowl_inns": "bowling_innings",
    "bowl_inning": "bowling_innings",
    "bb": "balls_bowled",
    "o": "overs_bowled",
    "ov": "overs_bowled",
    "ovs": "overs_bowled",
    "over": "overs_bowled",
    "overs": "overs_bowled",
    "ballsbowled": "balls_bowled",
    "bowl_balls": "balls_bowled",
    "runs_given": "runs_conceded",
    "runs_against": "runs_conceded",
    "conceded": "runs_conceded",
    "wkts": "wickets",
    "wkt": "wickets",
    "w": "wickets",
    "er": "economy",
    "m": "maidens",
    "mdns": "maidens",
    "m_bowling": "maidens",
    "best": "best_figures",
    "best_bowling": "best_figures",
    "best_figure": "best_figures",
    "best_figures": "best_figures",
    "bbf": "best_figures",
}
PLAYER_IMPORT_TABLE_FIELDS = set(PLAYER_IMPORT_FIELDS) | {"name", "best_figures", "serial", "overs_bowled"}
PLAYER_IMPORT_IGNORED_HEADERS = {
    "avg",
    "average",
    "ave",
    "strike_rate",
    "sr",
    "economy",
    "econ",
    "er",
    "centuries",
    "hundreds",
    "100",
    "fifties",
    "50",
}
SUPPORTED_IMPORT_EXTENSIONS = ".json, .csv, .pdf, .jpeg, .jpg, .png"


def placeholder_players(total):
    return ["Waiting for name" for _ in range(total)]


def build_batters(players):
    cleaned = [name.strip() for name in players if name and name.strip()]
    while len(cleaned) < 2:
        cleaned.append("Waiting for name")
    return [
        {
            "name": name,
            "runs": 0,
            "balls": 0,
            "fours": 0,
            "sixes": 0,
            "out": False,
            "retired": False,
            "dismissal": "not out",
        }
        for name in cleaned[:11]
    ]


def split_lines(value, fallback):
    if isinstance(value, str):
        lines = value.replace("\r", "").split("\n")
    else:
        lines = value or fallback
    cleaned = [line.strip() for line in lines if line and line.strip()]
    return cleaned or fallback


def blank_bowler(name):
    return {"name": name, "balls": 0, "maidens": 0, "runs": 0, "wickets": 0}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    global mongo_error
    init_sqlite_db()
    if not mongo_configured() or mongo_client is None:
        return
    try:
        mongo_client.admin.command("ping")
        mongo_error = None
    except PyMongoError as error:
        mongo_error = str(error)
        return
    try:
        players_collection.create_index([("name", ASCENDING)], unique=True)
        history_collection.create_index([("id", ASCENDING)], unique=True)
        history_collection.create_index([("played_at", DESCENDING)])
    except OperationFailure:
        # Some Atlas users can read/write but cannot manage indexes.
        # Upserts by name/id still keep the app's data model consistent.
        pass
    except PyMongoError as error:
        mongo_error = str(error)


def init_sqlite_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_stats (
                name TEXT PRIMARY KEY,
                matches INTEGER NOT NULL DEFAULT 0,
                batting_innings INTEGER NOT NULL DEFAULT 0,
                not_outs INTEGER NOT NULL DEFAULT 0,
                runs INTEGER NOT NULL DEFAULT 0,
                balls INTEGER NOT NULL DEFAULT 0,
                fours INTEGER NOT NULL DEFAULT 0,
                sixes INTEGER NOT NULL DEFAULT 0,
                ducks INTEGER NOT NULL DEFAULT 0,
                high_score INTEGER NOT NULL DEFAULT 0,
                bowling_innings INTEGER NOT NULL DEFAULT 0,
                balls_bowled INTEGER NOT NULL DEFAULT 0,
                runs_conceded INTEGER NOT NULL DEFAULT 0,
                wickets INTEGER NOT NULL DEFAULT 0,
                maidens INTEGER NOT NULL DEFAULT 0,
                best_wickets INTEGER NOT NULL DEFAULT 0,
                best_runs INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS match_history (
                id TEXT PRIMARY KEY,
                played_at TEXT NOT NULL,
                competition TEXT NOT NULL,
                venue TEXT NOT NULL,
                team_one TEXT NOT NULL,
                team_two TEXT NOT NULL,
                winner TEXT,
                result_text TEXT NOT NULL,
                innings_json TEXT NOT NULL
            )
            """
        )


def set_mongo_error(error):
    global mongo_error
    mongo_error = str(error)


def mongo_configured():
    return bool(MONGO_URI)


def using_mongo():
    return mongo_configured() and mongo_client is not None and not mongo_error


def mongo_writes_enabled():
    return using_mongo()


def mongo_status_message():
    if not mongo_configured():
        if derived_mongo_error:
            return f"MongoDB datasource found, but the app is using local SQLite. Error: {derived_mongo_error}"
        return "MongoDB is not configured. The app is using local SQLite. Add MONGODB_URI in .env to enable cloud storage."
    if mongo_error:
        return f"MongoDB is unavailable, so the app is using local SQLite. Error: {mongo_error}"
    return f"MongoDB connected. Database: {MONGO_DB_NAME}"


def base_player_doc(name):
    return {
        "name": name,
        "matches": 0,
        "batting_innings": 0,
        "not_outs": 0,
        "runs": 0,
        "balls": 0,
        "fours": 0,
        "sixes": 0,
        "ducks": 0,
        "high_score": 0,
        "bowling_innings": 0,
        "balls_bowled": 0,
        "runs_conceded": 0,
        "wickets": 0,
        "maidens": 0,
        "best_wickets": 0,
        "best_runs": 0,
    }


def valid_player_name(name):
    blocked_names = {"extras", "total", "fall of wickets", "score", "over"}
    return bool(name and name != "Waiting for name" and str(name).strip().lower() not in blocked_names)


def player_upsert_update(name, inc=None, max_values=None):
    base = base_player_doc(name)
    defaults = {field: {"$ifNull": [f"${field}", value]} for field, value in base.items()}
    defaults["name"] = name
    update = [{"$set": defaults}]
    stat_updates = {}
    if inc:
        stat_updates.update({field: {"$add": [f"${field}", value]} for field, value in inc.items()})
    if max_values:
        stat_updates.update({field: {"$max": [f"${field}", value]} for field, value in max_values.items()})
    if stat_updates:
        update.append({"$set": stat_updates})
    return update


def bulk_write_players(operations):
    if not operations or not mongo_writes_enabled():
        return
    try:
        players_collection.bulk_write(operations, ordered=True)
    except PyMongoError as error:
        set_mongo_error(error)


def bowling_pipeline_update(bowler):
    base = base_player_doc(bowler["name"])
    best_condition = {
        "$or": [
            {"$gt": [bowler["wickets"], {"$ifNull": ["$best_wickets", base["best_wickets"]]}]},
            {
                "$and": [
                    {"$eq": [bowler["wickets"], {"$ifNull": ["$best_wickets", base["best_wickets"]]}]},
                    {"$lt": [bowler["runs"], {"$ifNull": ["$best_runs", 999999]}]},
                ]
            },
        ]
    }
    defaults = {field: {"$ifNull": [f"${field}", value]} for field, value in base.items()}
    defaults["name"] = bowler["name"]
    return [
        {"$set": defaults},
        {
            "$set": {
                "bowling_innings": {"$add": ["$bowling_innings", 1]},
                "balls_bowled": {"$add": ["$balls_bowled", bowler["balls"]]},
                "runs_conceded": {"$add": ["$runs_conceded", bowler["runs"]]},
                "wickets": {"$add": ["$wickets", bowler["wickets"]]},
                "maidens": {"$add": ["$maidens", bowler["maidens"]]},
                "best_wickets": {"$cond": [best_condition, bowler["wickets"], "$best_wickets"]},
                "best_runs": {"$cond": [best_condition, bowler["runs"], "$best_runs"]},
            }
        },
    ]


def better_bowling_figures(candidate_wickets, candidate_runs, current_wickets, current_runs):
    if candidate_wickets > current_wickets:
        return True
    return candidate_wickets == current_wickets and candidate_runs < current_runs


def merge_player_totals(existing, imported):
    merged = {**base_player_doc(imported["name"]), **existing}
    for field in PLAYER_IMPORT_FIELDS:
        if field in {"high_score", "best_wickets", "best_runs"}:
            continue
        merged[field] = int(merged.get(field, 0)) + int(imported.get(field, 0))
    merged["high_score"] = max(int(existing.get("high_score", 0)), int(imported.get("high_score", 0)))
    current_best_wickets = int(existing.get("best_wickets", 0))
    current_best_runs = int(existing.get("best_runs", 0))
    imported_best_wickets = int(imported.get("best_wickets", 0))
    imported_best_runs = int(imported.get("best_runs", 0))
    if better_bowling_figures(imported_best_wickets, imported_best_runs, current_best_wickets, current_best_runs):
        merged["best_wickets"] = imported_best_wickets
        merged["best_runs"] = imported_best_runs
    else:
        merged["best_wickets"] = current_best_wickets
        merged["best_runs"] = current_best_runs
    return merged


def coerce_int(value, field_name):
    if value in (None, ""):
        return 0
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if cleaned in {"", "-", "--"}:
            return 0
        cleaned = cleaned.rstrip("*")
        if "." in cleaned:
            cleaned = cleaned.split(".", 1)[0]
        value = cleaned
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid integer for '{field_name}': {value}") from error


def normalize_import_header(header):
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(header or "").strip().lower()).strip("_")
    return PLAYER_IMPORT_HEADER_ALIASES.get(cleaned, cleaned)


def normalize_plaintext_headers(tokens):
    headers = []
    previous = None
    for token in tokens:
        header = normalize_import_header(token)
        if header == "name" and previous == "name":
            continue
        headers.append(header)
        previous = header
    return headers


def is_supported_plaintext_header(header):
    return header in PLAYER_IMPORT_TABLE_FIELDS or header in PLAYER_IMPORT_IGNORED_HEADERS


def is_plaintext_header_row(headers):
    if "name" not in headers:
        return False
    supported_headers = [header for header in headers if is_supported_plaintext_header(header)]
    stat_headers = [
        header
        for header in supported_headers
        if header not in {"name", "serial"} and header not in PLAYER_IMPORT_IGNORED_HEADERS
    ]
    return len(supported_headers) >= 2 and bool(stat_headers)


def clean_plaintext_stat_value(value):
    text = str(value or "").strip()
    if text in {"", "-", "--"}:
        return ""
    return text.replace(",", "")


def cricket_overs_to_balls(value):
    text = str(value or "").strip()
    if not text:
        return 0
    match = re.fullmatch(r"(\d+)(?:\.(\d))?", text)
    if not match:
        return coerce_int(value, "balls_bowled")
    overs = int(match.group(1))
    balls = int(match.group(2) or 0)
    if balls > 5:
        return overs
    return overs * 6 + balls


def is_scorecard_summary_line(line):
    first_cell = re.split(r"\s{2,}|\t+", line.strip(), maxsplit=1)[0].strip().lower()
    first_word = line.strip().split()[0].lower() if line.strip().split() else ""
    return first_cell in {"extras", "total", "fall of wickets"} or first_word in {"extras", "total"}


def is_bowling_plaintext_headers(headers):
    return "overs_bowled" in headers or "wickets" in headers or "maidens" in headers


def is_not_out_line(line):
    return line.strip().lower() in {"not out", "notout"}


def is_dismissal_detail_line(line):
    tokens = [token.strip() for token in re.split(r"\s+", line.strip()) if token.strip()]
    if not tokens:
        return False
    first = re.sub(r"[^a-z]", "", tokens[0].lower())
    return first in {"b", "c", "ct", "st", "lbw", "run", "retired"}


def is_numeric_stat_line(line):
    tokens = [token.strip() for token in re.split(r"\s+", line.strip()) if token.strip()]
    return bool(tokens) and all(re.fullmatch(r"[\d.,*]+", token) for token in tokens)


def trim_scorecard_dismissal_tokens(tokens):
    dismissal_markers = {
        "b",
        "c",
        "ct",
        "st",
        "lbw",
        "run",
        "not",
        "retired",
        "hit",
        "obstructing",
        "handled",
        "did",
    }
    for index, token in enumerate(tokens):
        cleaned = re.sub(r"[^a-z]", "", token.lower())
        if cleaned in dismissal_markers:
            return tokens[:index]
    return tokens


def row_from_split_batting_stats(name, stat_line, headers, not_out=False):
    trailing_headers = headers[headers.index("name") + 1:]
    values = [token.strip() for token in re.split(r"\s+", stat_line.strip()) if token.strip()]
    if len(values) < len(trailing_headers):
        return None

    row = {"name": name}
    for header, value in zip(trailing_headers, values):
        if header in PLAYER_IMPORT_FIELDS or header == "best_figures":
            row[header] = clean_plaintext_stat_value(value)
    if "runs" in row and "high_score" not in row:
        row["high_score"] = row["runs"]
    if "runs" in row or "balls" in row:
        row["batting_innings"] = "1"
        row["matches"] = "1"
    if not_out:
        row["not_outs"] = "1"
    return row


def merge_import_rows(existing, incoming):
    merged = {**existing}
    for field, value in incoming.items():
        if field == "name":
            continue
        if field == "high_score":
            merged[field] = str(max(coerce_int(merged.get(field, 0), field), coerce_int(value, field)))
        elif field == "matches":
            merged[field] = str(max(coerce_int(merged.get(field, 0), field), coerce_int(value, field)))
        elif field in {"best_wickets", "best_runs"}:
            merged[field] = value
        elif field in PLAYER_IMPORT_FIELDS:
            merged[field] = str(coerce_int(merged.get(field, 0), field) + coerce_int(value, field))
        else:
            merged[field] = value
    return merged


def parse_plaintext_player_row(line, headers):
    if is_scorecard_summary_line(line):
        return None

    tokens = [token.strip() for token in re.split(r"\s+", line.strip()) if token.strip()]
    if not tokens:
        return None

    name_index = headers.index("name")
    token_index = 0
    for header in headers[:name_index]:
        if token_index >= len(tokens):
            return None
        if header == "serial" and re.fullmatch(r"\d+[\).]?", tokens[token_index]):
            token_index += 1
        elif is_supported_plaintext_header(header):
            token_index += 1

    trailing_headers = headers[name_index + 1:]
    if len(tokens) - token_index <= len(trailing_headers):
        return None
    raw_name_tokens = tokens[token_index:len(tokens) - len(trailing_headers)]
    is_not_out = any(token.lower() == "not" for token in raw_name_tokens)
    name_tokens = raw_name_tokens
    name_tokens = trim_scorecard_dismissal_tokens(name_tokens)
    if not name_tokens:
        return None

    row = {"name": " ".join(name_tokens)}
    trailing_values = tokens[len(tokens) - len(trailing_headers):] if trailing_headers else []
    is_bowling_row = is_bowling_plaintext_headers(headers)
    for header, value in zip(trailing_headers, trailing_values):
        if header == "overs_bowled":
            row["balls_bowled"] = str(cricket_overs_to_balls(value))
            row["bowling_innings"] = "1"
        if header in PLAYER_IMPORT_FIELDS or header == "best_figures":
            row[header] = clean_plaintext_stat_value(value)
    if is_bowling_row and "runs" in row:
        row["runs_conceded"] = row.pop("runs")
    if is_bowling_row and "wickets" in row and "runs_conceded" in row:
        row["best_wickets"] = row["wickets"]
        row["best_runs"] = row["runs_conceded"]
    if "matches" not in row and (
        "batting_innings" in row or "bowling_innings" in row or "runs" in row or "runs_conceded" in row
    ):
        row["matches"] = "1"
    if not is_bowling_row and "runs" in row and "high_score" not in row:
        row["high_score"] = row["runs"]
    if not is_bowling_row and ("runs" in row or "balls" in row) and "batting_innings" not in row:
        row["batting_innings"] = "1"
    if is_not_out and "not_outs" not in row:
        row["not_outs"] = "1"
    return row


def parse_plaintext_player_rows(text):
    lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
    rows_by_name = {}
    headers = None
    previous_batter_key = None
    pending_batter_name = None
    pending_batter_not_out = False
    for line in lines:
        next_headers = normalize_plaintext_headers(re.split(r"\s+", line.strip()))
        if is_plaintext_header_row(next_headers):
            headers = next_headers
            previous_batter_key = None
            pending_batter_name = None
            pending_batter_not_out = False
            continue
        if headers is None:
            continue
        if previous_batter_key and not is_bowling_plaintext_headers(headers) and is_not_out_line(line):
            rows_by_name[previous_batter_key] = merge_import_rows(
                rows_by_name[previous_batter_key],
                {"name": rows_by_name[previous_batter_key]["name"], "not_outs": "1"},
            )
            continue
        if pending_batter_name and not is_bowling_plaintext_headers(headers):
            if is_not_out_line(line):
                pending_batter_not_out = True
                continue
            if is_dismissal_detail_line(line):
                continue
            if is_numeric_stat_line(line):
                row = row_from_split_batting_stats(pending_batter_name, line, headers, pending_batter_not_out)
                if row and valid_player_name(row.get("name")):
                    key = row["name"].strip().lower()
                    rows_by_name[key] = merge_import_rows(rows_by_name[key], row) if key in rows_by_name else row
                    previous_batter_key = key
                pending_batter_name = None
                pending_batter_not_out = False
                continue
            pending_batter_name = None
            pending_batter_not_out = False
        row = parse_plaintext_player_row(line, headers)
        if row and valid_player_name(row.get("name")):
            key = row["name"].strip().lower()
            rows_by_name[key] = merge_import_rows(rows_by_name[key], row) if key in rows_by_name else row
            previous_batter_key = key if not is_bowling_plaintext_headers(headers) else None
            pending_batter_name = None
            pending_batter_not_out = False
            continue
        if (
            not is_bowling_plaintext_headers(headers)
            and not is_scorecard_summary_line(line)
            and not is_not_out_line(line)
            and not is_dismissal_detail_line(line)
            and not re.search(r"\d", line)
        ):
            pending_batter_name = line.strip()
            pending_batter_not_out = False
    if rows_by_name:
        return list(rows_by_name.values())

    for header_index, line in enumerate(lines):
        headers = normalize_plaintext_headers(re.split(r"\s+", line.strip()))
        if not is_plaintext_header_row(headers):
            continue

        rows = []
        for data_line in lines[header_index + 1:]:
            if is_plaintext_header_row(normalize_plaintext_headers(re.split(r"\s+", data_line.strip()))):
                continue
            row = parse_plaintext_player_row(data_line, headers)
            if row and valid_player_name(row.get("name")):
                rows.append(row)
        if rows:
            return rows

    raise ValueError("Text import must include a player stats table with a 'name' column.")


def parse_best_figures(value):
    if value in (None, ""):
        return None
    text = str(value).strip()
    match = re.fullmatch(r"(\d+)\s*[/\-]\s*(\d+)", text)
    if not match:
        raise ValueError(f"Invalid best bowling figures: {value}")
    return int(match.group(1)), int(match.group(2))


def normalize_player_import_row(row):
    normalized_row = {}
    for key, value in (row or {}).items():
        normalized_key = normalize_import_header(key)
        if normalized_key:
            normalized_row[normalized_key] = value

    name = str(normalized_row.get("name", "")).strip()
    if not valid_player_name(name):
        raise ValueError("Each player row must include a valid 'name'.")
    normalized = base_player_doc(name)
    best_figures = parse_best_figures(normalized_row.get("best_figures"))
    for field in PLAYER_IMPORT_FIELDS:
        normalized[field] = coerce_int(normalized_row.get(field, 0), field)
    if best_figures:
        normalized["best_wickets"], normalized["best_runs"] = best_figures
    return normalized


def normalize_match_import_row(row):
    if not isinstance(row, dict):
        raise ValueError("Each match row must be a JSON object.")
    match_id = str(row.get("id", "")).strip()
    if not match_id:
        raise ValueError("Each match row must include 'id'.")
    innings = row.get("innings")
    if innings is None and row.get("innings_json"):
        try:
            innings = json.loads(row["innings_json"])
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid innings_json for match '{match_id}'.") from error
    if innings is None:
        innings = []
    if not isinstance(innings, list):
        raise ValueError(f"Match '{match_id}' has invalid 'innings'.")
    required_text_fields = ["played_at", "competition", "venue", "team_one", "team_two", "result_text"]
    normalized = {"id": match_id, "winner": row.get("winner"), "innings": innings}
    for field in required_text_fields:
        value = str(row.get(field, "")).strip()
        if not value:
            raise ValueError(f"Match '{match_id}' is missing '{field}'.")
        normalized[field] = value
    if normalized["winner"] is not None:
        normalized["winner"] = str(normalized["winner"]).strip() or None
    return normalized


def parse_json_import_payload(raw_bytes):
    try:
        text = raw_bytes.decode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Upload a valid UTF-8 JSON file.") from error
    return parse_json_import_text(text)


def parse_json_import_text(text):
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("Upload a valid JSON file.") from error

    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict) and "id" in payload[0] and "name" not in payload[0]:
            return [], [normalize_match_import_row(row) for row in payload]
        return [normalize_player_import_row(row) for row in payload], []

    if not isinstance(payload, dict):
        raise ValueError("JSON import must contain an object or an array.")

    player_rows = (
        payload.get("player_stats")
        or payload.get("players")
        or payload.get("batting")
        or []
    )
    match_rows = (
        payload.get("match_history")
        or payload.get("history")
        or payload.get("matches")
        or []
    )
    if not isinstance(player_rows, list) or not isinstance(match_rows, list):
        raise ValueError("JSON import lists must use arrays for players and match history.")
    return (
        [normalize_player_import_row(row) for row in player_rows],
        [normalize_match_import_row(row) for row in match_rows],
    )


def parse_csv_rows_from_text(text):
    cleaned_text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned_text:
        raise ValueError("The file does not contain any readable table data.")

    lines = [line.strip() for line in cleaned_text.split("\n") if line.strip()]
    candidates = [cleaned_text]

    whitespace_table = []
    whitespace_split = False
    for line in lines:
        parts = [part.strip() for part in re.split(r"\t+|\s{2,}", line) if part.strip()]
        if len(parts) > 1:
            whitespace_split = True
            whitespace_table.append(",".join(parts))
        else:
            whitespace_table.append(line)
    if whitespace_split:
        candidates.append("\n".join(whitespace_table))

    last_error = None
    for candidate in candidates:
        first_line = next((line for line in candidate.splitlines() if line.strip()), "")
        delimiter = ","
        for possible in (",", ";", "\t", "|"):
            if possible in first_line:
                delimiter = possible
                break
        reader = csv.DictReader(io.StringIO(candidate), delimiter=delimiter)
        fieldnames = [normalize_import_header(field) for field in (reader.fieldnames or []) if field and field.strip()]
        if not fieldnames:
            last_error = ValueError("CSV import must include a header row.")
            continue
        if "name" not in fieldnames:
            last_error = ValueError("CSV import must include a 'name' column.")
            continue

        rows = []
        for raw_row in reader:
            row = {}
            for key, value in raw_row.items():
                normalized_key = normalize_import_header(key)
                if normalized_key:
                    row[normalized_key] = value
            if any(str(value or "").strip() for value in row.values()):
                rows.append(row)
        return rows

    if last_error:
        try:
            return parse_plaintext_player_rows(cleaned_text)
        except ValueError:
            raise last_error
    raise ValueError("CSV import must include player rows.")


def parse_csv_import_text(text):
    return [normalize_player_import_row(row) for row in parse_csv_rows_from_text(text)], []


def parse_csv_import_payload(raw_bytes):
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Upload a valid UTF-8 CSV file.") from error
    return parse_csv_import_text(text)


def extract_pdf_text(raw_bytes):
    if PdfReader is None:
        raise ValueError("PDF import requires pypdf. Install the updated requirements first.")
    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
    except Exception as error:
        raise ValueError("The PDF file could not be read.") from error
    text = "\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
    if not text:
        raise ValueError(
            "The PDF does not contain extractable text. Use a text-based PDF or convert the file to CSV."
        )
    return text


def tesseract_ready():
    if pytesseract is None:
        return False
    command = getattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    if not command:
        return False
    return Path(command).exists() or shutil.which(command) is not None


def extract_image_text(raw_bytes):
    if Image is None or pytesseract is None:
        raise ValueError("Image import requires Pillow and pytesseract. Install the updated requirements first.")
    if not tesseract_ready():
        raise ValueError("Image import requires the Tesseract OCR app to be installed and available on PATH.")
    try:
        with Image.open(io.BytesIO(raw_bytes)) as image:
            prepared = image.convert("L")
            text = pytesseract.image_to_string(prepared)
    except Exception as error:
        raise ValueError("The image file could not be read.") from error
    text = text.strip()
    if not text:
        raise ValueError("No readable text was found in the image.")
    return text


def parse_extracted_text_import(text, source_label):
    cleaned_text = text.strip()
    if not cleaned_text:
        raise ValueError(f"The {source_label} file does not contain readable text.")
    if cleaned_text[:1] in {"{", "["}:
        try:
            return parse_json_import_text(cleaned_text)
        except ValueError:
            pass
    try:
        return parse_csv_import_text(cleaned_text)
    except ValueError as error:
        raise ValueError(
            f"The {source_label} text was read, but it is not valid JSON or a player stats table with a 'name' column."
        ) from error


def parse_import_upload(file_storage):
    filename = (file_storage.filename or "").strip()
    if not filename:
        raise ValueError("Choose a file to import.")
    raw_bytes = file_storage.read()
    if not raw_bytes:
        raise ValueError("The selected file is empty.")
    lower_name = filename.lower()
    if lower_name.endswith(".json"):
        return parse_json_import_payload(raw_bytes)
    if lower_name.endswith(".csv"):
        return parse_csv_import_payload(raw_bytes)
    if lower_name.endswith(".pdf"):
        return parse_extracted_text_import(extract_pdf_text(raw_bytes), "PDF")
    if lower_name.endswith((".jpeg", ".jpg", ".png")):
        return parse_extracted_text_import(extract_image_text(raw_bytes), "image")
    raise ValueError(f"Supported import formats are {SUPPORTED_IMPORT_EXTENSIONS}.")


def import_players_into_sqlite(player_rows):
    imported_count = 0
    with get_db() as conn:
        for row in player_rows:
            existing_row = conn.execute(
                "SELECT * FROM player_stats WHERE name = ?",
                (row["name"],),
            ).fetchone()
            existing = dict(existing_row) if existing_row else base_player_doc(row["name"])
            merged = merge_player_totals(existing, row)
            conn.execute(
                """
                INSERT INTO player_stats (
                    name, matches, batting_innings, not_outs, runs, balls, fours, sixes, ducks, high_score,
                    bowling_innings, balls_bowled, runs_conceded, wickets, maidens, best_wickets, best_runs
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    matches = excluded.matches,
                    batting_innings = excluded.batting_innings,
                    not_outs = excluded.not_outs,
                    runs = excluded.runs,
                    balls = excluded.balls,
                    fours = excluded.fours,
                    sixes = excluded.sixes,
                    ducks = excluded.ducks,
                    high_score = excluded.high_score,
                    bowling_innings = excluded.bowling_innings,
                    balls_bowled = excluded.balls_bowled,
                    runs_conceded = excluded.runs_conceded,
                    wickets = excluded.wickets,
                    maidens = excluded.maidens,
                    best_wickets = excluded.best_wickets,
                    best_runs = excluded.best_runs
                """,
                (
                    merged["name"],
                    merged["matches"],
                    merged["batting_innings"],
                    merged["not_outs"],
                    merged["runs"],
                    merged["balls"],
                    merged["fours"],
                    merged["sixes"],
                    merged["ducks"],
                    merged["high_score"],
                    merged["bowling_innings"],
                    merged["balls_bowled"],
                    merged["runs_conceded"],
                    merged["wickets"],
                    merged["maidens"],
                    merged["best_wickets"],
                    merged["best_runs"],
                ),
            )
            imported_count += 1
    return imported_count


def import_matches_into_sqlite(match_rows):
    imported_count = 0
    with get_db() as conn:
        for row in match_rows:
            conn.execute(
                """
                INSERT INTO match_history (
                    id, played_at, competition, venue, team_one, team_two, winner, result_text, innings_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    played_at = excluded.played_at,
                    competition = excluded.competition,
                    venue = excluded.venue,
                    team_one = excluded.team_one,
                    team_two = excluded.team_two,
                    winner = excluded.winner,
                    result_text = excluded.result_text,
                    innings_json = excluded.innings_json
                """,
                (
                    row["id"],
                    row["played_at"],
                    row["competition"],
                    row["venue"],
                    row["team_one"],
                    row["team_two"],
                    row["winner"],
                    row["result_text"],
                    json.dumps(row["innings"]),
                ),
            )
            imported_count += 1
    return imported_count


def import_players_into_mongo(player_rows):
    imported_count = 0
    for row in player_rows:
        existing = clean_mongo_doc(players_collection.find_one({"name": row["name"]}, {"_id": 0})) or base_player_doc(row["name"])
        merged = merge_player_totals(existing, row)
        players_collection.replace_one({"name": row["name"]}, merged, upsert=True)
        imported_count += 1
    return imported_count


def import_matches_into_mongo(match_rows):
    if not match_rows:
        return 0
    history_collection.bulk_write(
        [UpdateOne({"id": row["id"]}, {"$set": row}, upsert=True) for row in match_rows],
        ordered=False,
    )
    return len(match_rows)


def import_stats_file(file_storage):
    player_rows, match_rows = parse_import_upload(file_storage)
    if not player_rows and not match_rows:
        raise ValueError("The file does not contain any player stats or match history rows.")
    if using_mongo():
        imported_players = import_players_into_mongo(player_rows)
        imported_matches = import_matches_into_mongo(match_rows)
        return imported_players, imported_matches, "MongoDB"
    imported_players = import_players_into_sqlite(player_rows)
    imported_matches = import_matches_into_sqlite(match_rows)
    return imported_players, imported_matches, "SQLite"


def ensure_sqlite_player(conn, name):
    if valid_player_name(name):
        conn.execute("INSERT OR IGNORE INTO player_stats (name) VALUES (?)", (name,))


def add_sqlite_match_played(conn, name):
    ensure_sqlite_player(conn, name)
    conn.execute("UPDATE player_stats SET matches = matches + 1 WHERE name = ?", (name,))


def add_sqlite_batting_stats(conn, batter):
    ensure_sqlite_player(conn, batter["name"])
    not_out = 0 if batter["out"] else 1
    duck = 1 if batter["out"] and batter["runs"] == 0 else 0
    conn.execute(
        """
        UPDATE player_stats
        SET batting_innings = batting_innings + 1,
            not_outs = not_outs + ?,
            runs = runs + ?,
            balls = balls + ?,
            fours = fours + ?,
            sixes = sixes + ?,
            ducks = ducks + ?,
            high_score = CASE WHEN ? > high_score THEN ? ELSE high_score END
        WHERE name = ?
        """,
        (
            not_out,
            batter["runs"],
            batter["balls"],
            batter["fours"],
            batter["sixes"],
            duck,
            batter["runs"],
            batter["runs"],
            batter["name"],
        ),
    )


def add_sqlite_bowling_stats(conn, bowler):
    if bowler["balls"] == 0 and bowler["runs"] == 0 and bowler["wickets"] == 0:
        return
    ensure_sqlite_player(conn, bowler["name"])
    conn.execute(
        """
        UPDATE player_stats
        SET bowling_innings = bowling_innings + 1,
            balls_bowled = balls_bowled + ?,
            runs_conceded = runs_conceded + ?,
            wickets = wickets + ?,
            maidens = maidens + ?,
            best_wickets = CASE
                WHEN ? > best_wickets OR (? = best_wickets AND ? < best_runs) THEN ?
                ELSE best_wickets
            END,
            best_runs = CASE
                WHEN ? > best_wickets OR (? = best_wickets AND ? < best_runs) THEN ?
                ELSE best_runs
            END
        WHERE name = ?
        """,
        (
            bowler["balls"],
            bowler["runs"],
            bowler["wickets"],
            bowler["maidens"],
            bowler["wickets"],
            bowler["wickets"],
            bowler["runs"],
            bowler["wickets"],
            bowler["wickets"],
            bowler["wickets"],
            bowler["runs"],
            bowler["runs"],
            bowler["name"],
        ),
    )


def ensure_player(name):
    if not valid_player_name(name) or not mongo_writes_enabled():
        return
    try:
        players_collection.update_one(
            {"name": name},
            player_upsert_update(name),
            upsert=True,
        )
    except PyMongoError as error:
        set_mongo_error(error)


def add_match_played(name):
    if not valid_player_name(name) or not mongo_writes_enabled():
        return
    try:
        players_collection.update_one({"name": name}, player_upsert_update(name, {"matches": 1}), upsert=True)
    except PyMongoError as error:
        set_mongo_error(error)


def add_batting_stats(batter):
    if not valid_player_name(batter["name"]) or not mongo_writes_enabled():
        return
    not_out = 0 if batter["out"] else 1
    duck = 1 if batter["out"] and batter["runs"] == 0 else 0
    try:
        players_collection.update_one(
            {"name": batter["name"]},
            player_upsert_update(
                batter["name"],
                {
                    "batting_innings": 1,
                    "not_outs": not_out,
                    "runs": batter["runs"],
                    "balls": batter["balls"],
                    "fours": batter["fours"],
                    "sixes": batter["sixes"],
                    "ducks": duck,
                },
                {"high_score": batter["runs"]},
            ),
            upsert=True,
        )
    except PyMongoError as error:
        set_mongo_error(error)


def add_bowling_stats(bowler):
    if bowler["balls"] == 0 and bowler["runs"] == 0 and bowler["wickets"] == 0:
        return
    if not valid_player_name(bowler["name"]) or not mongo_writes_enabled():
        return
    try:
        players_collection.update_one({"name": bowler["name"]}, bowling_pipeline_update(bowler), upsert=True)
    except PyMongoError as error:
        set_mongo_error(error)


def clean_mongo_doc(doc):
    if not doc:
        return doc
    cleaned = dict(doc)
    cleaned.pop("_id", None)
    return cleaned


def player_stats():
    rows = []
    if not using_mongo():
        with get_db() as conn:
            sqlite_rows = conn.execute(
                """
                SELECT *,
                    CASE WHEN batting_innings - not_outs > 0
                        THEN ROUND(CAST(runs AS REAL) / (batting_innings - not_outs), 2)
                        ELSE runs END AS batting_average,
                    CASE WHEN balls > 0 THEN ROUND(CAST(runs AS REAL) * 100 / balls, 2) ELSE 0 END AS strike_rate,
                    CASE WHEN balls_bowled > 0 THEN ROUND(CAST(runs_conceded AS REAL) * 6 / balls_bowled, 2) ELSE 0 END AS economy,
                    CASE WHEN wickets > 0 THEN ROUND(CAST(runs_conceded AS REAL) / wickets, 2) ELSE 0 END AS bowling_average
                FROM player_stats
                ORDER BY runs DESC, wickets DESC, name ASC
                """
            ).fetchall()
        return [dict(row) for row in sqlite_rows]
    try:
        cursor = players_collection.find({}, {"_id": 0}).sort([("runs", DESCENDING), ("wickets", DESCENDING), ("name", ASCENDING)])
        for doc in cursor:
            row = {**base_player_doc(doc["name"]), **doc}
            dismissals = row["batting_innings"] - row["not_outs"]
            row["batting_average"] = round(row["runs"] / dismissals, 2) if dismissals > 0 else row["runs"]
            row["strike_rate"] = round(row["runs"] * 100 / row["balls"], 2) if row["balls"] > 0 else 0
            row["economy"] = round(row["runs_conceded"] * 6 / row["balls_bowled"], 2) if row["balls_bowled"] > 0 else 0
            row["bowling_average"] = round(row["runs_conceded"] / row["wickets"], 2) if row["wickets"] > 0 else 0
            rows.append(row)
    except PyMongoError as error:
        set_mongo_error(error)
    return rows


def batting_leaders():
    rows = player_stats()
    return sorted(rows, key=lambda row: (-row["runs"], -row["high_score"], row["name"]))


def bowling_leaders():
    rows = player_stats()
    return sorted(rows, key=lambda row: (-row["wickets"], row["economy"], row["name"]))


def match_history():
    if not using_mongo():
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM match_history
                ORDER BY played_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]
    try:
        return list(history_collection.find({}, {"_id": 0, "innings": 0}).sort("played_at", DESCENDING))
    except PyMongoError as error:
        set_mongo_error(error)
        return []


def match_detail(match_id):
    if not using_mongo():
        with get_db() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM match_history
                WHERE id = ?
                """,
                (match_id,),
            ).fetchone()
        if not row:
            return None
        detail = dict(row)
        detail["innings"] = json.loads(detail["innings_json"])
        return detail
    try:
        detail = clean_mongo_doc(history_collection.find_one({"id": match_id}))
    except PyMongoError as error:
        set_mongo_error(error)
        return None
    if not detail:
        return None
    detail["innings"] = detail.get("innings", [])
    return detail


def delete_player_stat(name):
    if not valid_player_name(name):
        return False
    if not using_mongo():
        with get_db() as conn:
            cursor = conn.execute("DELETE FROM player_stats WHERE name = ?", (name,))
            return cursor.rowcount > 0
    try:
        result = players_collection.delete_one({"name": name})
        return result.deleted_count > 0
    except PyMongoError as error:
        set_mongo_error(error)
        return False


def delete_match_history(match_id):
    if not match_id:
        return False
    if not using_mongo():
        with get_db() as conn:
            cursor = conn.execute("DELETE FROM match_history WHERE id = ?", (match_id,))
            return cursor.rowcount > 0
    try:
        result = history_collection.delete_one({"id": match_id})
        return result.deleted_count > 0
    except PyMongoError as error:
        set_mongo_error(error)
        return False


def can_migrate_sqlite_to_mongo():
    if not using_mongo():
        return False
    with get_db() as conn:
        player_count = conn.execute("SELECT COUNT(*) FROM player_stats").fetchone()[0]
        history_count = conn.execute("SELECT COUNT(*) FROM match_history").fetchone()[0]
    return player_count > 0 or history_count > 0


def migrate_sqlite_to_mongo():
    if not using_mongo():
        return 0, 0
    with get_db() as conn:
        player_rows = [dict(row) for row in conn.execute("SELECT * FROM player_stats").fetchall()]
        history_rows = [dict(row) for row in conn.execute("SELECT * FROM match_history").fetchall()]

    if player_rows:
        players_collection.bulk_write(
            [
                UpdateOne(
                    {"name": row["name"]},
                    {"$set": row},
                    upsert=True,
                )
                for row in player_rows
            ],
            ordered=False,
        )

    migrated_matches = []
    for row in history_rows:
        innings = []
        if row.get("innings_json"):
            try:
                innings = json.loads(row["innings_json"])
            except json.JSONDecodeError:
                innings = []
        migrated_matches.append(
            {
                "id": row["id"],
                "played_at": row["played_at"],
                "competition": row["competition"],
                "venue": row["venue"],
                "team_one": row["team_one"],
                "team_two": row["team_two"],
                "winner": row["winner"],
                "result_text": row["result_text"],
                "innings": innings,
            }
        )

    if migrated_matches:
        history_collection.bulk_write(
            [
                UpdateOne(
                    {"id": row["id"]},
                    {"$set": row},
                    upsert=True,
                )
                for row in migrated_matches
            ],
            ordered=False,
        )
    return len(player_rows), len(migrated_matches)


init_db()


def fresh_match(settings=None, innings=1, target=None, innings_scores=None):
    settings = settings or {}
    team_count = settings.get("team_count", 2)
    players_per_team = settings.get("players_per_team", 11)
    team_names = settings.get("team_names") or [
        settings.get("batting_team", ""),
        settings.get("bowling_team", ""),
    ]
    while len(team_names) < 2:
        team_names.append("")

    team_rosters = settings.get("team_rosters") or {
        team_names[0]: placeholder_players(players_per_team),
        team_names[1]: placeholder_players(players_per_team),
    }
    batting_team = settings.get("batting_team", team_names[0])
    bowling_team = settings.get("bowling_team", team_names[1])
    batters = build_batters(team_rosters.get(batting_team, DEFAULT_PLAYERS))
    return {
        "competition": settings.get("competition", "Premier T20 League"),
        "match_id": settings.get("match_id", str(uuid4())),
        "match_players_recorded": settings.get("match_players_recorded", False),
        "venue": settings.get("venue", "National Cricket Ground"),
        "toss_winner": settings.get("toss_winner", ""),
        "toss_decision": settings.get("toss_decision", ""),
        "team_count": team_count,
        "players_per_team": players_per_team,
        "team_names": team_names,
        "team_rosters": team_rosters,
        "innings": innings,
        "innings_scores": innings_scores or [],
        "innings_complete": False,
        "match_complete": False,
        "history_recorded": settings.get("history_recorded", False),
        "result": None,
        "batting_team": batting_team,
        "bowling_team": bowling_team,
        "target": target,
        "max_overs": settings.get("max_overs", 20),
        "score": 0,
        "wickets": 0,
        "legal_balls": 0,
        "extras": {"wd": 0, "nb": 0, "b": 0, "lb": 0},
        "batters": batters,
        "fall_of_wickets": [],
        "striker": 0,
        "non_striker": 1,
        "next_batter": 2,
        "bowler": blank_bowler(settings.get("bowler", "")),
        "bowling_figures": {},
        "stats_recorded": False,
        "pending_batter": True,
        "pending_batter_slot": "striker",
        "pending_batter_index": 0,
        "pending_bowler": False,
        "over_events": [],
        "timeline": [],
    }


match_data = fresh_match()


def overs_text(legal_balls):
    return f"{legal_balls // 6}.{legal_balls % 6}"


def run_rate():
    if match_data["legal_balls"] == 0:
        return "0.00"
    return f"{match_data['score'] / (match_data['legal_balls'] / 6):.2f}"


def batter_sr(batter):
    if batter["balls"] == 0:
        return "0.00"
    return f"{batter['runs'] * 100 / batter['balls']:.2f}"


def bowler_economy():
    if match_data["bowler"]["balls"] == 0:
        return "0.00"
    return f"{match_data['bowler']['runs'] / (match_data['bowler']['balls'] / 6):.2f}"


def current_over_score():
    return sum(event["runs"] for event in match_data["over_events"])


def projected_score():
    rate = float(run_rate())
    return int(rate * match_data["max_overs"])


def sync_current_bowler():
    match_data["bowling_figures"][match_data["bowler"]["name"]] = deepcopy(match_data["bowler"])


def record_match_players_once():
    if match_data["match_players_recorded"]:
        return
    names = set()
    for roster in match_data["team_rosters"].values():
        names.update(name for name in roster if valid_player_name(name))
    if not using_mongo():
        with get_db() as conn:
            for name in sorted(names):
                add_sqlite_match_played(conn, name)
        match_data["match_players_recorded"] = True
        return
    bulk_write_players(
        [
            UpdateOne({"name": name}, player_upsert_update(name, {"matches": 1}), upsert=True)
            for name in sorted(names)
        ]
    )
    match_data["match_players_recorded"] = True


def record_innings_stats_once():
    if match_data["stats_recorded"]:
        return
    sync_current_bowler()
    record_match_players_once()
    if not using_mongo():
        with get_db() as conn:
            for batter in match_data["batters"]:
                if valid_player_name(batter["name"]) and (batter["balls"] > 0 or batter["runs"] > 0 or batter["out"]):
                    add_sqlite_batting_stats(conn, batter)
            for bowler in match_data["bowling_figures"].values():
                if valid_player_name(bowler["name"]):
                    add_sqlite_bowling_stats(conn, bowler)
        match_data["stats_recorded"] = True
        return
    operations = []
    for batter in match_data["batters"]:
        if batter["balls"] > 0 or batter["runs"] > 0 or batter["out"]:
            if not valid_player_name(batter["name"]):
                continue
            not_out = 0 if batter["out"] else 1
            duck = 1 if batter["out"] and batter["runs"] == 0 else 0
            operations.append(
                UpdateOne(
                    {"name": batter["name"]},
                    player_upsert_update(
                        batter["name"],
                        {
                            "batting_innings": 1,
                            "not_outs": not_out,
                            "runs": batter["runs"],
                            "balls": batter["balls"],
                            "fours": batter["fours"],
                            "sixes": batter["sixes"],
                            "ducks": duck,
                        },
                        {"high_score": batter["runs"]},
                    ),
                    upsert=True,
                )
            )
    for bowler in match_data["bowling_figures"].values():
        if (
            valid_player_name(bowler["name"])
            and (bowler["balls"] > 0 or bowler["runs"] > 0 or bowler["wickets"] > 0)
        ):
            operations.append(
                UpdateOne(
                    {"name": bowler["name"]},
                    bowling_pipeline_update(bowler),
                    upsert=True,
                )
            )
    bulk_write_players(operations)
    match_data["stats_recorded"] = True


def innings_scorecard(reason):
    sync_current_bowler()
    return {
        "team": match_data["batting_team"],
        "bowling_team": match_data["bowling_team"],
        "score": match_data["score"],
        "wickets": match_data["wickets"],
        "overs": overs_text(match_data["legal_balls"]),
        "reason": reason,
        "extras": deepcopy(match_data["extras"]),
        "batters": deepcopy(match_data["batters"]),
        "bowlers": list(deepcopy(match_data["bowling_figures"]).values()),
        "fall_of_wickets": deepcopy(match_data["fall_of_wickets"]),
    }


def set_match_result():
    if match_data["innings"] < 2 or not match_data["innings_scores"]:
        return

    first = match_data["innings_scores"][0]
    first_team = first["team"]
    chasing_team = match_data["batting_team"]
    first_score = first["score"]
    chasing_score = match_data["score"]

    if chasing_score > first_score:
        wickets_left = len(match_data["batters"]) - 1 - match_data["wickets"]
        match_data["result"] = {
            "winner": chasing_team,
            "margin": f"won by {wickets_left} wicket{'s' if wickets_left != 1 else ''}",
            "text": f"{chasing_team} won by {wickets_left} wicket{'s' if wickets_left != 1 else ''}",
        }
    elif chasing_score == first_score:
        match_data["result"] = {
            "winner": None,
            "margin": "Match tied",
            "text": "Match tied",
        }
    else:
        runs_margin = first_score - chasing_score
        match_data["result"] = {
            "winner": first_team,
            "margin": f"won by {runs_margin} run{'s' if runs_margin != 1 else ''}",
            "text": f"{first_team} won by {runs_margin} run{'s' if runs_margin != 1 else ''}",
        }


def record_match_history_once():
    if match_data["history_recorded"] or not match_data["match_complete"] or not match_data["result"]:
        return
    if not using_mongo():
        with get_db() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO match_history (
                    id, played_at, competition, venue, team_one, team_two, winner, result_text, innings_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match_data["match_id"],
                    datetime.now().isoformat(timespec="seconds"),
                    match_data["competition"],
                    match_data["venue"],
                    match_data["team_names"][0],
                    match_data["team_names"][1],
                    match_data["result"]["winner"],
                    match_data["result"]["text"],
                    json.dumps(match_data["innings_scores"]),
                ),
            )
        match_data["history_recorded"] = True
        return
    try:
        history_collection.update_one(
            {"id": match_data["match_id"]},
            {
                "$setOnInsert": {
                    "id": match_data["match_id"],
                    "played_at": datetime.now().isoformat(timespec="seconds"),
                    "competition": match_data["competition"],
                    "venue": match_data["venue"],
                    "team_one": match_data["team_names"][0],
                    "team_two": match_data["team_names"][1],
                    "winner": match_data["result"]["winner"],
                    "result_text": match_data["result"]["text"],
                    "innings": deepcopy(match_data["innings_scores"]),
                }
            },
            upsert=True,
        )
        match_data["history_recorded"] = True
    except PyMongoError as error:
        set_mongo_error(error)


def finish_innings(reason):
    if match_data["innings_complete"]:
        return
    match_data["innings_complete"] = True
    match_data["innings_scores"].append(
        innings_scorecard(reason)
    )
    record_innings_stats_once()
    if match_data["innings"] >= 2:
        set_match_result()
        match_data["match_complete"] = True
        record_match_history_once()


def maybe_finish_innings():
    if match_data["wickets"] >= len(match_data["batters"]) - 1:
        finish_innings("All out")
    elif match_data["legal_balls"] >= match_data["max_overs"] * 6:
        finish_innings("Overs complete")
    elif match_data["target"] and match_data["score"] >= match_data["target"]:
        finish_innings("Target reached")


def add_event(label, runs, legal=True, kind=""):
    match_data["over_events"].append({"label": label, "runs": runs, "legal": legal, "kind": kind})
    if len(match_data["over_events"]) > 10:
        match_data["over_events"] = match_data["over_events"][-10:]

    match_data["timeline"].insert(
        0,
        {
            "over": overs_text(match_data["legal_balls"]),
            "label": label,
            "runs": runs,
            "kind": kind,
            "score": f"{match_data['score']}/{match_data['wickets']}",
        },
    )
    match_data["timeline"] = match_data["timeline"][:12]


def rotate_strike():
    match_data["striker"], match_data["non_striker"] = match_data["non_striker"], match_data["striker"]


def complete_legal_ball():
    match_data["legal_balls"] += 1
    match_data["bowler"]["balls"] += 1
    if match_data["legal_balls"] % 6 == 0:
        if current_over_score() == 0:
            match_data["bowler"]["maidens"] += 1
        match_data["over_events"] = []
        rotate_strike()
        match_data["pending_bowler"] = True


def response_state():
    if match_data["match_complete"] and match_data["result"] and not match_data["history_recorded"]:
        record_match_history_once()

    striker = match_data["batters"][match_data["striker"]]
    non_striker = match_data["batters"][match_data["non_striker"]]
    data = deepcopy(match_data)
    data["overs"] = overs_text(match_data["legal_balls"])
    data["run_rate"] = run_rate()
    data["projected_score"] = projected_score()
    data["striker_data"] = {**striker, "sr": batter_sr(striker)}
    data["non_striker_data"] = {**non_striker, "sr": batter_sr(non_striker)}
    data["bowler"]["overs"] = overs_text(match_data["bowler"]["balls"])
    data["bowler"]["economy"] = bowler_economy()
    data["balls_remaining"] = max((match_data["max_overs"] * 6) - match_data["legal_balls"], 0)
    data["all_out_at"] = max(len(match_data["batters"]) - 1, 1)
    data["history_saved"] = match_data["history_recorded"]
    if match_data.get("toss_winner") and match_data.get("toss_decision"):
        data["toss_text"] = f"{match_data['toss_winner']} won the toss and chose to {match_data['toss_decision']}"
    else:
        data["toss_text"] = "Toss not set"
    return data


@app.route("/")
def home():
    return render_template("index.html", data=response_state())


@app.route("/state")
def state():
    return jsonify(response_state())


@app.route("/stats")
def stats_page():
    all_players = sorted(player_stats(), key=lambda row: (row["name"].lower(), -row["runs"], -row["wickets"]))
    return render_template(
        "stats.html",
        batting=sorted(all_players, key=lambda row: (-row["runs"], -row["high_score"], row["name"])),
        bowling=sorted(all_players, key=lambda row: (-row["wickets"], row["economy"], row["name"])),
        all_players=all_players,
        history=match_history(),
        mongo_status=mongo_status_message(),
        can_migrate=can_migrate_sqlite_to_mongo(),
        migration_message=request.args.get("migration"),
    )


@app.route("/history/<match_id>")
def history_scorecard(match_id):
    match = match_detail(match_id)
    if not match:
        return "Match not found", 404
    return render_template("match_detail.html", match=match)


@app.route("/players/<path:name>/delete", methods=["POST"])
def delete_player_stat_route(name):
    deleted = delete_player_stat(name)
    message = f"Deleted player stat for {name}." if deleted else f"Player stat for {name} was not found."
    return redirect(url_for("stats_page", migration=message))


@app.route("/history/<match_id>/delete", methods=["POST"])
def delete_match_history_route(match_id):
    deleted = delete_match_history(match_id)
    message = "Deleted match history entry." if deleted else "Match history entry was not found."
    return redirect(url_for("stats_page", migration=message))


@app.route("/stats.json")
def stats_json():
    all_players = sorted(player_stats(), key=lambda row: (row["name"].lower(), -row["runs"], -row["wickets"]))
    return jsonify(
        {
            "batting": sorted(all_players, key=lambda row: (-row["runs"], -row["high_score"], row["name"])),
            "bowling": sorted(all_players, key=lambda row: (-row["wickets"], row["economy"], row["name"])),
            "all_players": all_players,
            "history": match_history(),
            "mongo_status": mongo_status_message(),
            "can_migrate": can_migrate_sqlite_to_mongo(),
        }
    )


@app.route("/migrate_to_mongo", methods=["POST"])
def migrate_to_mongo_route():
    if not using_mongo():
        return redirect(url_for("stats_page", migration="MongoDB is not connected yet. Add MONGODB_URI in .env first."))
    try:
        migrated_players, migrated_matches = migrate_sqlite_to_mongo()
        message = f"Migrated {migrated_players} player records and {migrated_matches} matches into MongoDB."
    except PyMongoError as error:
        set_mongo_error(error)
        message = f"MongoDB migration failed: {error}"
    return redirect(url_for("stats_page", migration=message))


@app.route("/import_stats", methods=["POST"])
def import_stats_route():
    uploaded_file = request.files.get("import_file")
    if not uploaded_file:
        return redirect(url_for("stats_page", migration=f"Choose one of these formats before importing: {SUPPORTED_IMPORT_EXTENSIONS}."))
    try:
        imported_players, imported_matches, target = import_stats_file(uploaded_file)
        message = (
            f"Imported {imported_players} player rows and {imported_matches} match rows into {target}. "
            "Player totals were merged by player name and matches were upserted by match id."
        )
    except ValueError as error:
        message = f"Import failed: {error}"
    except PyMongoError as error:
        set_mongo_error(error)
        message = f"Import failed: {error}"
    return redirect(url_for("stats_page", migration=message))


@app.route("/reset_stats", methods=["POST"])
def reset_stats():
    if not using_mongo():
        with get_db() as conn:
            conn.execute("DELETE FROM player_stats")
            conn.execute("DELETE FROM match_history")
        return redirect(url_for("stats_page"))
    try:
        players_collection.delete_many({})
        history_collection.delete_many({})
    except PyMongoError as error:
        set_mongo_error(error)
    return redirect(url_for("stats_page"))


@app.route("/score", methods=["POST"])
def score():
    if (
        match_data["innings_complete"]
        or match_data["match_complete"]
        or match_data["pending_batter"]
        or match_data["pending_bowler"]
    ):
        return jsonify(response_state())

    payload = request.get_json(silent=True) or request.form
    runs = int(payload.get("runs", 0))
    extra = payload.get("extra", "")
    wicket = str(payload.get("wicket", "false")).lower() == "true"

    striker = match_data["batters"][match_data["striker"]]
    dismissed_index = match_data["striker"]
    legal = extra not in {"wd", "nb"}
    batting_runs = 0 if extra in {"wd", "nb", "b", "lb"} else runs
    total_runs = runs

    if extra == "wd":
        total_runs = runs + 1
        match_data["extras"]["wd"] += total_runs
    elif extra == "nb":
        total_runs = runs + 1
        batting_runs = runs
        match_data["extras"]["nb"] += 1
    elif extra in {"b", "lb"}:
        match_data["extras"][extra] += runs

    match_data["score"] += total_runs
    match_data["bowler"]["runs"] += total_runs

    if legal:
        striker["balls"] += 1
    striker["runs"] += batting_runs
    if batting_runs == 4:
        striker["fours"] += 1
    if batting_runs == 6:
        striker["sixes"] += 1

    label = f"{runs}" if not extra else f"{runs}{extra.upper()}"

    event_kind = "four" if batting_runs == 4 else "six" if batting_runs == 6 else ""

    if wicket and match_data["wickets"] < len(match_data["batters"]) - 1:
        event_kind = "wicket"
        match_data["wickets"] += 1
        match_data["bowler"]["wickets"] += 1
        striker["out"] = True
        striker["dismissal"] = f"b {match_data['bowler']['name']}"
        wicket_over = overs_text(match_data["legal_balls"] + (1 if legal else 0))
        match_data["fall_of_wickets"].append(
            {
                "batter": striker["name"],
                "score": f"{match_data['score']}/{match_data['wickets']}",
                "over": wicket_over,
            }
        )
        label = f"W {label}" if total_runs else "W"

        if match_data["next_batter"] < len(match_data["batters"]) and match_data["wickets"] < len(match_data["batters"]) - 1:
            match_data["pending_batter"] = True
            match_data["pending_batter_index"] = match_data["next_batter"]

    if not wicket and runs % 2 == 1 and match_data["legal_balls"] % 6 != 0:
        rotate_strike()

    add_event(label, total_runs, legal, event_kind)

    if legal:
        complete_legal_ball()

    maybe_finish_innings()
    if match_data["pending_batter"] and not match_data["innings_complete"]:
        match_data["pending_batter_slot"] = (
            "striker" if match_data["striker"] == dismissed_index else "non_striker"
        )

    return jsonify(response_state())


@app.route("/new_batter", methods=["POST"])
def new_batter():
    if not match_data["pending_batter"] or match_data["innings_complete"] or match_data["match_complete"]:
        return jsonify(response_state())

    payload = request.get_json(silent=True) or request.form
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Batter name is required", **response_state()}), 400
    next_index = match_data.get("pending_batter_index")
    if next_index is None:
        next_index = match_data["next_batter"]
    if next_index < len(match_data["batters"]):
        if name:
            match_data["batters"][next_index]["name"] = name
        if next_index < 2:
            if next_index == 0:
                match_data["striker"] = next_index
                match_data["pending_batter_slot"] = "non_striker"
                match_data["pending_batter_index"] = 1
                return jsonify(response_state())
            match_data["non_striker"] = next_index
        else:
            if match_data["pending_batter_slot"] == "non_striker":
                match_data["non_striker"] = next_index
            else:
                match_data["striker"] = next_index
            match_data["next_batter"] += 1
    match_data["pending_batter"] = False
    match_data["pending_batter_slot"] = None
    match_data["pending_batter_index"] = None
    if next_index == 1 and not match_data["bowler"]["name"]:
        match_data["pending_bowler"] = True
    return jsonify(response_state())


@app.route("/retire_batter", methods=["POST"])
def retire_batter():
    if (
        match_data["innings_complete"]
        or match_data["match_complete"]
        or match_data["pending_batter"]
        or match_data["pending_bowler"]
        or match_data["next_batter"] >= len(match_data["batters"])
    ):
        return jsonify(response_state())

    payload = request.get_json(silent=True) or request.form
    slot = payload.get("slot", "striker")
    retiring_index = match_data["non_striker"] if slot == "non_striker" else match_data["striker"]
    batter = match_data["batters"][retiring_index]
    batter["retired"] = True
    batter["dismissal"] = "retired"

    match_data["pending_batter"] = True
    match_data["pending_batter_slot"] = "non_striker" if slot == "non_striker" else "striker"
    match_data["pending_batter_index"] = match_data["next_batter"]
    match_data["timeline"].insert(
        0,
        {
            "over": overs_text(match_data["legal_balls"]),
            "label": f"{batter['name']} retired",
            "runs": 0,
            "score": f"{match_data['score']}/{match_data['wickets']}",
        },
    )
    match_data["timeline"] = match_data["timeline"][:12]
    return jsonify(response_state())


@app.route("/new_bowler", methods=["POST"])
def new_bowler():
    if not match_data["pending_bowler"] or match_data["innings_complete"] or match_data["match_complete"]:
        return jsonify(response_state())

    payload = request.get_json(silent=True) or request.form
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Bowler name is required", **response_state()}), 400
    sync_current_bowler()
    match_data["bowler"] = deepcopy(match_data["bowling_figures"].get(name, blank_bowler(name)))
    match_data["pending_bowler"] = False
    return jsonify(response_state())


@app.route("/reset", methods=["POST"])
def reset():
    global match_data
    match_data = fresh_match()
    return jsonify(response_state())


@app.route("/next_innings", methods=["POST"])
def next_innings():
    global match_data
    if not match_data["innings_complete"] or match_data["match_complete"]:
        return jsonify(response_state())

    previous = deepcopy(match_data)
    settings = {
        "competition": previous["competition"],
        "match_id": previous["match_id"],
        "match_players_recorded": previous["match_players_recorded"],
        "venue": previous["venue"],
        "toss_winner": previous["toss_winner"],
        "toss_decision": previous["toss_decision"],
        "team_count": previous["team_count"],
        "players_per_team": previous["players_per_team"],
        "team_names": previous["team_names"],
        "team_rosters": previous["team_rosters"],
        "batting_team": previous["bowling_team"],
        "bowling_team": previous["batting_team"],
        "max_overs": previous["max_overs"],
        "bowler": "",
    }
    match_data = fresh_match(
        settings,
        innings=previous["innings"] + 1,
        target=previous["score"] + 1,
        innings_scores=previous["innings_scores"],
    )
    return jsonify(response_state())


@app.route("/setup", methods=["POST"])
def setup():
    global match_data
    payload = request.get_json(silent=True) or request.form
    extra_team_names = split_lines(payload.get("team_names", ""), [])

    try:
        max_overs = int(payload.get("max_overs", 20))
    except (TypeError, ValueError):
        max_overs = 20

    max_overs = min(max(max_overs, 1), 50)
    try:
        team_count = int(payload.get("team_count", 2))
    except (TypeError, ValueError):
        team_count = 2
    team_count = min(max(team_count, 2), 16)
    try:
        players_per_team = int(payload.get("players_per_team", 11))
    except (TypeError, ValueError):
        players_per_team = 11
    players_per_team = min(max(players_per_team, 2), 11)

    batting_team = (payload.get("batting_team") or "").strip()
    bowling_team = (payload.get("bowling_team") or "").strip()
    bowler = (payload.get("bowler") or "").strip()
    toss_winner = (payload.get("toss_winner") or "").strip()
    toss_decision = (payload.get("toss_decision") or "").strip()
    if not batting_team or not bowling_team:
        return jsonify({"error": "Batting team and bowling team names are required"}), 400

    team_names = [batting_team, bowling_team]
    for name in extra_team_names:
        if name not in team_names:
            team_names.append(name)
    if len(team_names) < team_count:
        return jsonify({"error": f"Enter {team_count} team names before starting"}), 400

    settings = {
        "competition": payload.get("competition") or "Custom Match",
        "venue": payload.get("venue") or "Local Ground",
        "toss_winner": toss_winner or batting_team,
        "toss_decision": toss_decision if toss_decision in {"bat", "field"} else "bat",
        "team_count": team_count,
        "players_per_team": players_per_team,
        "team_names": team_names[:team_count],
        "team_rosters": {
            batting_team: placeholder_players(players_per_team),
            bowling_team: placeholder_players(players_per_team),
        },
        "batting_team": batting_team,
        "bowling_team": bowling_team,
        "max_overs": max_overs,
        "bowler": bowler,
    }
    match_data = fresh_match(settings)
    return jsonify(response_state())


if __name__ == "__main__":
    app.run(debug=True)
