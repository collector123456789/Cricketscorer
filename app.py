from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from flask import Flask, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)
DB_PATH = Path(__file__).with_name("cricket_stats.db")


DEFAULT_PLAYERS = []
DEFAULT_OPPONENT_PLAYERS = []


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


def ensure_player(conn, name):
    conn.execute("INSERT OR IGNORE INTO player_stats (name) VALUES (?)", (name,))


def add_match_played(conn, name):
    ensure_player(conn, name)
    conn.execute("UPDATE player_stats SET matches = matches + 1 WHERE name = ?", (name,))


def add_batting_stats(conn, batter):
    ensure_player(conn, batter["name"])
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


def add_bowling_stats(conn, bowler):
    if bowler["balls"] == 0 and bowler["runs"] == 0 and bowler["wickets"] == 0:
        return
    ensure_player(conn, bowler["name"])
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


def player_stats():
    with get_db() as conn:
        rows = conn.execute(
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
    return [dict(row) for row in rows]


def batting_leaders():
    rows = player_stats()
    return sorted(rows, key=lambda row: (-row["runs"], -row["high_score"], row["name"]))


def bowling_leaders():
    rows = player_stats()
    return sorted(rows, key=lambda row: (-row["wickets"], row["economy"], row["name"]))


def match_history():
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM match_history
            ORDER BY played_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def match_detail(match_id):
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
        names.update(name for name in roster if name and name != "Waiting for name")
    with get_db() as conn:
        for name in names:
            add_match_played(conn, name)
    match_data["match_players_recorded"] = True


def record_innings_stats_once():
    if match_data["stats_recorded"]:
        return
    sync_current_bowler()
    record_match_players_once()
    with get_db() as conn:
        for batter in match_data["batters"]:
            if batter["balls"] > 0 or batter["runs"] > 0 or batter["out"]:
                add_batting_stats(conn, batter)
        for bowler in match_data["bowling_figures"].values():
            add_bowling_stats(conn, bowler)
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
    return render_template(
        "stats.html",
        batting=batting_leaders(),
        bowling=bowling_leaders(),
        history=match_history(),
    )


@app.route("/history/<match_id>")
def history_scorecard(match_id):
    match = match_detail(match_id)
    if not match:
        return "Match not found", 404
    return render_template("match_detail.html", match=match)


@app.route("/stats.json")
def stats_json():
    return jsonify(
        {
            "batting": batting_leaders(),
            "bowling": bowling_leaders(),
            "history": match_history(),
        }
    )


@app.route("/reset_stats", methods=["POST"])
def reset_stats():
    with get_db() as conn:
        conn.execute("DELETE FROM player_stats")
        conn.execute("DELETE FROM match_history")
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
    match_data["pending_bowler"] = True
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
    if not bowler:
        return jsonify({"error": "Opening bowler name is required"}), 400

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
