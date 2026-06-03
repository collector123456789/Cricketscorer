package com.example.cricketscorer

import android.app.Activity
import android.app.AlertDialog
import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID
import kotlin.math.max
import kotlin.math.min

private val GREEN = 0xFF0B7A4B.toInt()
private val GREEN_DARK = 0xFF06452F.toInt()
private val BLUE = 0xFF165D8F.toInt()
private val RED = 0xFFB42318.toInt()
private val BG = 0xFFEEF2F0.toInt()
private val SURFACE = Color.WHITE
private val SOFT = 0xFFF7FAF8.toInt()

data class Batter(
    var name: String,
    var runs: Int = 0,
    var balls: Int = 0,
    var fours: Int = 0,
    var sixes: Int = 0,
    var out: Boolean = false,
    var retired: Boolean = false,
    var dismissal: String = "not out"
)

data class Bowler(
    var name: String,
    var balls: Int = 0,
    var maidens: Int = 0,
    var runs: Int = 0,
    var wickets: Int = 0
)

data class BallEvent(
    val over: String,
    val label: String,
    val runs: Int,
    val score: String,
    val kind: String = ""
)

data class InningsScore(
    val team: String,
    val bowlingTeam: String,
    val score: Int,
    val wickets: Int,
    val overs: String,
    val reason: String,
    val batters: List<Batter>,
    val bowlers: List<Bowler>
)

data class PlayerRow(
    val name: String,
    val matches: Int,
    val battingInnings: Int,
    val notOuts: Int,
    val runs: Int,
    val balls: Int,
    val fours: Int,
    val sixes: Int,
    val ducks: Int,
    val highScore: Int,
    val bowlingInnings: Int,
    val ballsBowled: Int,
    val runsConceded: Int,
    val wickets: Int,
    val maidens: Int,
    val bestWickets: Int,
    val bestRuns: Int
) {
    val battingAverage: String
        get() = if (battingInnings - notOuts > 0) "%.2f".format(runs.toDouble() / (battingInnings - notOuts)) else runs.toString()
    val strikeRate: String
        get() = if (balls > 0) "%.2f".format(runs * 100.0 / balls) else "0.00"
    val economy: String
        get() = if (ballsBowled > 0) "%.2f".format(runsConceded * 6.0 / ballsBowled) else "0.00"
    val bowlingAverage: String
        get() = if (wickets > 0) "%.2f".format(runsConceded.toDouble() / wickets) else "0.00"
}

class StatsDb(context: Context) : SQLiteOpenHelper(context, "cricket_stats.db", null, 1) {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE player_stats (
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
            """.trimIndent()
        )
        db.execSQL(
            """
            CREATE TABLE match_history (
                id TEXT PRIMARY KEY,
                played_at TEXT NOT NULL,
                competition TEXT NOT NULL,
                venue TEXT NOT NULL,
                team_one TEXT NOT NULL,
                team_two TEXT NOT NULL,
                winner TEXT,
                result_text TEXT NOT NULL
            )
            """.trimIndent()
        )
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit

    private fun ensurePlayer(db: SQLiteDatabase, name: String) {
        if (name.isBlank() || name == "Waiting for name") return
        db.insertWithOnConflict("player_stats", null, ContentValues().apply { put("name", name) }, SQLiteDatabase.CONFLICT_IGNORE)
    }

    fun addMatchPlayed(names: Set<String>) {
        writableDatabase.beginTransaction()
        try {
            names.forEach { name ->
                ensurePlayer(writableDatabase, name)
                writableDatabase.execSQL("UPDATE player_stats SET matches = matches + 1 WHERE name = ?", arrayOf(name))
            }
            writableDatabase.setTransactionSuccessful()
        } finally {
            writableDatabase.endTransaction()
        }
    }

    fun addInningsStats(batters: List<Batter>, bowlers: List<Bowler>) {
        writableDatabase.beginTransaction()
        try {
            batters.filter { it.name != "Waiting for name" && (it.balls > 0 || it.runs > 0 || it.out) }.forEach { b ->
                ensurePlayer(writableDatabase, b.name)
                val notOut = if (b.out) 0 else 1
                val duck = if (b.out && b.runs == 0) 1 else 0
                writableDatabase.execSQL(
                    """
                    UPDATE player_stats SET
                        batting_innings = batting_innings + 1,
                        not_outs = not_outs + ?,
                        runs = runs + ?,
                        balls = balls + ?,
                        fours = fours + ?,
                        sixes = sixes + ?,
                        ducks = ducks + ?,
                        high_score = CASE WHEN ? > high_score THEN ? ELSE high_score END
                    WHERE name = ?
                    """.trimIndent(),
                    arrayOf(notOut, b.runs, b.balls, b.fours, b.sixes, duck, b.runs, b.runs, b.name)
                )
            }
            bowlers.filter { it.name.isNotBlank() && (it.balls > 0 || it.runs > 0 || it.wickets > 0) }.forEach { bw ->
                ensurePlayer(writableDatabase, bw.name)
                writableDatabase.execSQL(
                    """
                    UPDATE player_stats SET
                        bowling_innings = bowling_innings + 1,
                        balls_bowled = balls_bowled + ?,
                        runs_conceded = runs_conceded + ?,
                        wickets = wickets + ?,
                        maidens = maidens + ?,
                        best_wickets = CASE WHEN ? > best_wickets OR (? = best_wickets AND ? < best_runs) THEN ? ELSE best_wickets END,
                        best_runs = CASE WHEN ? > best_wickets OR (? = best_wickets AND ? < best_runs) THEN ? ELSE best_runs END
                    WHERE name = ?
                    """.trimIndent(),
                    arrayOf(
                        bw.balls, bw.runs, bw.wickets, bw.maidens,
                        bw.wickets, bw.wickets, bw.runs, bw.wickets,
                        bw.wickets, bw.wickets, bw.runs, bw.runs,
                        bw.name
                    )
                )
            }
            writableDatabase.setTransactionSuccessful()
        } finally {
            writableDatabase.endTransaction()
        }
    }

    fun saveHistory(id: String, competition: String, venue: String, teamOne: String, teamTwo: String, winner: String?, resultText: String) {
        val values = ContentValues().apply {
            put("id", id)
            put("played_at", SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date()))
            put("competition", competition)
            put("venue", venue)
            put("team_one", teamOne)
            put("team_two", teamTwo)
            put("winner", winner)
            put("result_text", resultText)
        }
        writableDatabase.insertWithOnConflict("match_history", null, values, SQLiteDatabase.CONFLICT_IGNORE)
    }

    fun players(orderBy: String): List<PlayerRow> {
        val rows = mutableListOf<PlayerRow>()
        readableDatabase.rawQuery("SELECT * FROM player_stats ORDER BY $orderBy", null).use { c ->
            while (c.moveToNext()) {
                rows += PlayerRow(
                    c.getString(c.getColumnIndexOrThrow("name")),
                    c.getInt(c.getColumnIndexOrThrow("matches")),
                    c.getInt(c.getColumnIndexOrThrow("batting_innings")),
                    c.getInt(c.getColumnIndexOrThrow("not_outs")),
                    c.getInt(c.getColumnIndexOrThrow("runs")),
                    c.getInt(c.getColumnIndexOrThrow("balls")),
                    c.getInt(c.getColumnIndexOrThrow("fours")),
                    c.getInt(c.getColumnIndexOrThrow("sixes")),
                    c.getInt(c.getColumnIndexOrThrow("ducks")),
                    c.getInt(c.getColumnIndexOrThrow("high_score")),
                    c.getInt(c.getColumnIndexOrThrow("bowling_innings")),
                    c.getInt(c.getColumnIndexOrThrow("balls_bowled")),
                    c.getInt(c.getColumnIndexOrThrow("runs_conceded")),
                    c.getInt(c.getColumnIndexOrThrow("wickets")),
                    c.getInt(c.getColumnIndexOrThrow("maidens")),
                    c.getInt(c.getColumnIndexOrThrow("best_wickets")),
                    c.getInt(c.getColumnIndexOrThrow("best_runs"))
                )
            }
        }
        return rows
    }

    fun history(): List<String> {
        val rows = mutableListOf<String>()
        readableDatabase.rawQuery("SELECT * FROM match_history ORDER BY played_at DESC", null).use { c ->
            while (c.moveToNext()) {
                rows += "${c.getString(c.getColumnIndexOrThrow("played_at"))}\n" +
                    "${c.getString(c.getColumnIndexOrThrow("competition"))} - ${c.getString(c.getColumnIndexOrThrow("venue"))}\n" +
                    "${c.getString(c.getColumnIndexOrThrow("team_one"))} vs ${c.getString(c.getColumnIndexOrThrow("team_two"))}\n" +
                    c.getString(c.getColumnIndexOrThrow("result_text"))
            }
        }
        return rows
    }

    fun resetAll() {
        writableDatabase.execSQL("DELETE FROM player_stats")
        writableDatabase.execSQL("DELETE FROM match_history")
    }
}

class MatchEngine(private val db: StatsDb) {
    var competition = "Premier T20 League"
    var venue = "National Cricket Ground"
    var tossWinner = ""
    var tossDecision = ""
    var teamNames = mutableListOf("Team A", "Team B")
    var rosters = mutableMapOf<String, MutableList<String>>()
    var battingTeam = "Team A"
    var bowlingTeam = "Team B"
    var maxOvers = 20
    var playersPerTeam = 11
    var innings = 1
    var matchId = UUID.randomUUID().toString()
    var target: Int? = null
    var score = 0
    var wickets = 0
    var legalBalls = 0
    var extras = mutableMapOf("wd" to 0, "nb" to 0, "b" to 0, "lb" to 0)
    var batters = mutableListOf<Batter>()
    var striker = 0
    var nonStriker = 1
    var nextBatter = 2
    var bowler = Bowler("")
    var bowlingFigures = mutableMapOf<String, Bowler>()
    var overEvents = mutableListOf<BallEvent>()
    var timeline = mutableListOf<BallEvent>()
    var inningsScores = mutableListOf<InningsScore>()
    var inningsComplete = false
    var matchComplete = false
    var resultText = ""
    var resultWinner: String? = null
    var pendingBatter = true
    var pendingBatterSlot = "striker"
    var pendingBatterIndex = 0
    var pendingBowler = false
    private var matchPlayersRecorded = false
    private var statsRecorded = false
    private var historyRecorded = false

    init {
        fresh()
    }

    fun setup(
        competitionInput: String,
        venueInput: String,
        batting: String,
        bowling: String,
        oversInput: Int,
        openingBowler: String,
        toss: String,
        decision: String,
        playerCount: Int
    ): String? {
        if (batting.isBlank() || bowling.isBlank()) return "Batting and bowling team names are required"
        if (openingBowler.isBlank()) return "Opening bowler is required"
        competition = competitionInput.ifBlank { "Custom Match" }
        venue = venueInput.ifBlank { "Local Ground" }
        battingTeam = batting.trim()
        bowlingTeam = bowling.trim()
        teamNames = mutableListOf(battingTeam, bowlingTeam)
        tossWinner = toss.ifBlank { battingTeam }
        tossDecision = if (decision == "field") "field" else "bat"
        maxOvers = min(max(oversInput, 1), 50)
        playersPerTeam = min(max(playerCount, 2), 11)
        matchId = UUID.randomUUID().toString()
        target = null
        innings = 1
        matchPlayersRecorded = false
        historyRecorded = false
        inningsScores.clear()
        fresh()
        bowler = Bowler(openingBowler.trim())
        pendingBowler = false
        return null
    }

    private fun fresh() {
        rosters = mutableMapOf(
            battingTeam to MutableList(playersPerTeam) { "Waiting for name" },
            bowlingTeam to MutableList(playersPerTeam) { "Waiting for name" }
        )
        score = 0
        wickets = 0
        legalBalls = 0
        extras = mutableMapOf("wd" to 0, "nb" to 0, "b" to 0, "lb" to 0)
        batters = MutableList(playersPerTeam) { Batter("Waiting for name") }
        striker = 0
        nonStriker = 1
        nextBatter = 2
        bowler = Bowler("")
        bowlingFigures = mutableMapOf()
        overEvents = mutableListOf()
        timeline = mutableListOf()
        inningsComplete = false
        matchComplete = false
        resultText = ""
        resultWinner = null
        pendingBatter = true
        pendingBatterSlot = "striker"
        pendingBatterIndex = 0
        pendingBowler = false
        statsRecorded = false
        historyRecorded = false
    }

    fun oversText(balls: Int = legalBalls) = "${balls / 6}.${balls % 6}"
    fun runRate() = if (legalBalls == 0) "0.00" else "%.2f".format(score / (legalBalls / 6.0))
    fun projectedScore() = (runRate().toDoubleOrNull() ?: 0.0).times(maxOvers).toInt()
    fun totalExtras() = extras.values.sum()
    fun ballsRemaining() = max(maxOvers * 6 - legalBalls, 0)
    fun batterSr(b: Batter) = if (b.balls == 0) "0.00" else "%.2f".format(b.runs * 100.0 / b.balls)
    fun bowlerEconomy(b: Bowler = bowler) = if (b.balls == 0) "0.00" else "%.2f".format(b.runs / (b.balls / 6.0))

    fun addOpeningOrIncomingBatter(name: String): String? {
        if (!pendingBatter || inningsComplete || matchComplete) return null
        if (name.isBlank()) return "Batter name is required"
        val index = pendingBatterIndex
        if (index < batters.size) {
            batters[index].name = name.trim()
            rosters.getOrPut(battingTeam) { mutableListOf() }.let {
                while (it.size <= index) it += "Waiting for name"
                it[index] = name.trim()
            }
            if (index == 0) {
                striker = 0
                pendingBatterSlot = "non_striker"
                pendingBatterIndex = 1
                return null
            }
            if (index == 1) {
                nonStriker = 1
            } else {
                if (pendingBatterSlot == "non_striker") nonStriker = index else striker = index
                nextBatter += 1
            }
        }
        pendingBatter = false
        pendingBatterIndex = -1
        return null
    }

    fun addNewBowler(name: String): String? {
        if (!pendingBowler || inningsComplete || matchComplete) return null
        if (name.isBlank()) return "Bowler name is required"
        syncBowler()
        bowler = bowlingFigures[name.trim()]?.copy() ?: Bowler(name.trim())
        pendingBowler = false
        return null
    }

    fun retire(slot: String) {
        if (inningsComplete || matchComplete || pendingBatter || pendingBowler || nextBatter >= batters.size) return
        val index = if (slot == "non_striker") nonStriker else striker
        batters[index].retired = true
        batters[index].dismissal = "retired"
        pendingBatter = true
        pendingBatterSlot = slot
        pendingBatterIndex = nextBatter
        addTimeline("${batters[index].name} retired", 0, "")
    }

    fun scoreBall(runs: Int, extra: String, wicket: Boolean) {
        if (inningsComplete || matchComplete || pendingBatter || pendingBowler) return
        val strikerBefore = striker
        val legal = extra !in setOf("wd", "nb")
        var battingRuns = if (extra in setOf("wd", "nb", "b", "lb")) 0 else runs
        var totalRuns = runs

        when (extra) {
            "wd" -> {
                totalRuns = runs + 1
                extras["wd"] = extras.getValue("wd") + totalRuns
            }
            "nb" -> {
                totalRuns = runs + 1
                battingRuns = runs
                extras["nb"] = extras.getValue("nb") + 1
            }
            "b", "lb" -> extras[extra] = extras.getValue(extra) + runs
        }

        val batter = batters[striker]
        score += totalRuns
        bowler.runs += totalRuns
        if (legal) batter.balls += 1
        batter.runs += battingRuns
        if (battingRuns == 4) batter.fours += 1
        if (battingRuns == 6) batter.sixes += 1

        var label = if (extra.isBlank()) "$runs" else "$runs${extra.uppercase(Locale.US)}"
        var kind = when (battingRuns) {
            4 -> "four"
            6 -> "six"
            else -> ""
        }

        if (wicket && wickets < batters.size - 1) {
            kind = "wicket"
            wickets += 1
            bowler.wickets += 1
            batter.out = true
            batter.dismissal = "b ${bowler.name}"
            label = if (totalRuns > 0) "W $label" else "W"
            if (nextBatter < batters.size && wickets < batters.size - 1) {
                pendingBatter = true
                pendingBatterIndex = nextBatter
            }
        }

        if (!wicket && runs % 2 == 1 && legalBalls % 6 != 0) rotateStrike()
        addBallEvent(label, totalRuns, kind)
        if (legal) completeLegalBall()
        maybeFinishInnings()

        if (pendingBatter && !inningsComplete) {
            pendingBatterSlot = if (striker == strikerBefore) "striker" else "non_striker"
        }
    }

    fun nextInnings() {
        if (!inningsComplete || matchComplete) return
        val previousScore = score
        val oldBatting = battingTeam
        battingTeam = bowlingTeam.also { bowlingTeam = oldBatting }
        innings += 1
        target = previousScore + 1
        fresh()
        pendingBowler = true
    }

    fun reset() {
        competition = "Premier T20 League"
        venue = "National Cricket Ground"
        battingTeam = "Team A"
        bowlingTeam = "Team B"
        teamNames = mutableListOf(battingTeam, bowlingTeam)
        maxOvers = 20
        innings = 1
        target = null
        matchId = UUID.randomUUID().toString()
        matchPlayersRecorded = false
        inningsScores.clear()
        fresh()
    }

    private fun syncBowler() {
        if (bowler.name.isNotBlank()) bowlingFigures[bowler.name] = bowler.copy()
    }

    private fun currentOverScore() = overEvents.sumOf { it.runs }

    private fun rotateStrike() {
        val old = striker
        striker = nonStriker
        nonStriker = old
    }

    private fun completeLegalBall() {
        legalBalls += 1
        bowler.balls += 1
        if (legalBalls % 6 == 0) {
            if (currentOverScore() == 0) bowler.maidens += 1
            overEvents.clear()
            rotateStrike()
            pendingBowler = true
        }
    }

    private fun addBallEvent(label: String, runs: Int, kind: String) {
        val event = BallEvent(oversText(), label, runs, "$score/$wickets", kind)
        overEvents += event
        if (overEvents.size > 10) overEvents = overEvents.takeLast(10).toMutableList()
        timeline.add(0, event)
        if (timeline.size > 12) timeline = timeline.take(12).toMutableList()
    }

    private fun addTimeline(label: String, runs: Int, kind: String) {
        timeline.add(0, BallEvent(oversText(), label, runs, "$score/$wickets", kind))
        if (timeline.size > 12) timeline = timeline.take(12).toMutableList()
    }

    private fun maybeFinishInnings() {
        when {
            wickets >= batters.size - 1 -> finishInnings("All out")
            legalBalls >= maxOvers * 6 -> finishInnings("Overs complete")
            target != null && score >= target!! -> finishInnings("Target reached")
        }
    }

    private fun finishInnings(reason: String) {
        if (inningsComplete) return
        syncBowler()
        inningsComplete = true
        inningsScores += InningsScore(battingTeam, bowlingTeam, score, wickets, oversText(), reason, batters.map { it.copy() }, bowlingFigures.values.map { it.copy() })
        recordStatsOnce()
        if (innings >= 2) {
            setResult()
            matchComplete = true
            recordHistoryOnce()
        }
    }

    private fun recordStatsOnce() {
        if (statsRecorded) return
        if (!matchPlayersRecorded) {
            val names = rosters.values.flatten().filter { it.isNotBlank() && it != "Waiting for name" }.toSet()
            db.addMatchPlayed(names)
            matchPlayersRecorded = true
        }
        db.addInningsStats(batters, bowlingFigures.values.toList())
        statsRecorded = true
    }

    private fun setResult() {
        val first = inningsScores.firstOrNull() ?: return
        resultText = when {
            score > first.score -> {
                resultWinner = battingTeam
                val wicketsLeft = batters.size - 1 - wickets
                "$battingTeam won by $wicketsLeft wicket${if (wicketsLeft == 1) "" else "s"}"
            }
            score == first.score -> {
                resultWinner = null
                "Match tied"
            }
            else -> {
                resultWinner = first.team
                val margin = first.score - score
                "${first.team} won by $margin run${if (margin == 1) "" else "s"}"
            }
        }
    }

    private fun recordHistoryOnce() {
        if (historyRecorded || !matchComplete || resultText.isBlank()) return
        db.saveHistory(matchId, competition, venue, teamNames[0], teamNames[1], resultWinner, resultText)
        historyRecorded = true
    }
}

class MainActivity : Activity() {
    private lateinit var db: StatsDb
    private lateinit var match: MatchEngine
    private lateinit var root: LinearLayout
    private var selectedExtra = ""
    private var wicketMode = false
    private var setupTossWinner: String? = null
    private var setupTossDecision: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        db = StatsDb(this)
        match = MatchEngine(db)
        showScorer()
    }

    private fun showScorer() {
        root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(12), dp(12), dp(24))
            setBackgroundColor(BG)
        }
        setContentView(ScrollView(this).apply { addView(root) })
        renderScorer()
    }

    private fun renderScorer() {
        root.removeAllViews()
        root.addView(row {
            addView(titleBlock("${match.battingTeam} vs ${match.bowlingTeam}", "${match.competition}\n${match.venue} - Innings ${match.innings}"))
            addView(button("Stats", BLUE) { showStats() })
            addView(button("Reset", RED) {
                match.reset()
                setupTossWinner = null
                setupTossDecision = null
                renderScorer()
            })
        })

        root.addView(card {
            addView(text("Match Setup", 20, GREEN_DARK, true))
            val competition = edit(match.competition, "Competition")
            val venue = edit(match.venue, "Venue")
            val teamOne = edit(match.teamNames.getOrElse(0) { match.battingTeam }, "Team 1")
            val teamTwo = edit(match.teamNames.getOrElse(1) { match.bowlingTeam }, "Team 2")
            val overs = edit(match.maxOvers.toString(), "Overs")
            val bowler = edit(match.bowler.name, "Opening bowler")
            val players = edit(match.playersPerTeam.toString(), "Players per team")
            listOf(competition, venue, teamOne, teamTwo, overs, bowler, players).forEach { addView(it) }

            val tossStatus = text("Flip the coin after entering both teams.", 16, BLUE, true)
            val decisionRow = wrapRow {
                visibility = View.GONE
                addView(button("Bat first", GREEN) {
                    setupTossDecision = "bat"
                    tossStatus.text = "${setupTossWinner ?: "Toss winner"} chose to bat first"
                })
                addView(button("Bowl first", BLUE) {
                    setupTossDecision = "field"
                    tossStatus.text = "${setupTossWinner ?: "Toss winner"} chose to bowl first"
                })
            }
            addView(tossStatus)
            addView(button("Flip coin", BLUE) {
                val first = teamOne.text.toString().trim()
                val second = teamTwo.text.toString().trim()
                if (first.isBlank() || second.isBlank()) {
                    toast("Enter both team names before the toss")
                } else {
                    setupTossWinner = null
                    setupTossDecision = null
                    decisionRow.visibility = View.GONE
                    animateCoinFlip(first, second, tossStatus) { winner ->
                        setupTossWinner = winner
                        tossStatus.text = "$winner won the toss. Choose bat or bowl."
                        decisionRow.visibility = View.VISIBLE
                    }
                }
            })
            addView(decisionRow)
            addView(button("Start match", GREEN) {
                val first = teamOne.text.toString().trim()
                val second = teamTwo.text.toString().trim()
                val winner = setupTossWinner
                val decision = setupTossDecision
                if (winner == null || winner !in listOf(first, second)) {
                    toast("Flip the coin before starting the match")
                    return@button
                }
                if (decision == null) {
                    toast("Choose bat or bowl first")
                    return@button
                }
                val other = if (winner == first) second else first
                val battingTeam = if (decision == "bat") winner else other
                val bowlingTeam = if (decision == "bat") other else winner
                val error = match.setup(
                    competition.text.toString(),
                    venue.text.toString(),
                    battingTeam,
                    bowlingTeam,
                    overs.text.toString().toIntOrNull() ?: 20,
                    bowler.text.toString(),
                    winner,
                    decision,
                    players.text.toString().toIntOrNull() ?: 11
                )
                if (error != null) {
                    toast(error)
                } else {
                    setupTossWinner = null
                    setupTossDecision = null
                    renderScorer()
                }
            })
        })

        root.addView(scoreHero())

        if (match.inningsComplete) {
            root.addView(card {
                val last = match.inningsScores.last()
                addView(text(if (match.matchComplete) "Match complete" else "Innings complete", 20, RED, true))
                addView(text("${last.team} ${last.score}/${last.wickets} in ${last.overs} overs - ${last.reason}", 16))
                if (match.matchComplete) addView(text(match.resultText, 18, GREEN_DARK, true)) else {
                    addView(button("Start next innings", GREEN) {
                        match.nextInnings()
                        renderScorer()
                    })
                }
            })
        }

        if (match.pendingBatter && !match.inningsComplete) {
            root.addView(promptCard("New batter required", if (match.pendingBatterIndex < 2) "Opening ${match.pendingBatterSlot.replace("_", "-")} name" else "Incoming batter name") { name ->
                val error = match.addOpeningOrIncomingBatter(name)
                if (error != null) toast(error)
                renderScorer()
            })
        }

        if (match.pendingBowler && !match.inningsComplete) {
            root.addView(promptCard("New bowler required", "Next bowler name") { name ->
                val error = match.addNewBowler(name)
                if (error != null) toast(error)
                renderScorer()
            })
        }

        root.addView(battingCard())
        root.addView(scorePad())
        root.addView(bowlingCard())
        root.addView(listCard("Current Over", match.overEvents.map { it.label }.ifEmpty { listOf("New over ready") }))
        root.addView(listCard("Scorecard", match.batters.mapIndexed { i, b ->
            val status = when {
                i == match.striker -> "*"
                i == match.nonStriker -> "ns"
                b.out -> "out"
                b.retired -> "retired"
                else -> ""
            }
            "${b.name}  ${b.runs} (${b.balls}) $status"
        }))
        root.addView(listCard("Ball Log", match.timeline.map { "${it.over}  ${it.label}  ${it.score}" }.ifEmpty { listOf("No balls scored yet") }))
    }

    private fun scoreHero(): View = card(GREEN_DARK) {
        addView(text(match.battingTeam, 14, Color.WHITE, true))
        addView(text("${match.score}/${match.wickets}", 48, Color.WHITE, true))
        addView(text("Overs ${match.oversText()}  CRR ${match.runRate()}  Target ${match.target ?: "--"}", 16, Color.WHITE))
        val tossLine = if (match.tossWinner.isBlank()) "Toss not set" else "${match.tossWinner} chose to ${if (match.tossDecision == "field") "bowl" else "bat"}"
        addView(text(tossLine, 15, Color.WHITE))
        addView(text("Projected ${match.projectedScore()}  Remaining ${match.ballsRemaining()} balls  Extras ${match.totalExtras()}", 16, Color.WHITE))
    }

    private fun battingCard(): View = card {
        addView(text("Batting", 20, GREEN_DARK, true))
        val s = match.batters[match.striker]
        val ns = match.batters[match.nonStriker]
        addView(text("Striker: ${s.name}  ${s.runs} (${s.balls})  4s ${s.fours}  6s ${s.sixes}  SR ${match.batterSr(s)}", 16, GREEN_DARK, true))
        addView(button("Retire striker", RED) {
            match.retire("striker")
            renderScorer()
        })
        addView(text("Non-striker: ${ns.name}  ${ns.runs} (${ns.balls})  4s ${ns.fours}  6s ${ns.sixes}  SR ${match.batterSr(ns)}", 16))
        addView(button("Retire non-striker", RED) {
            match.retire("non_striker")
            renderScorer()
        })
    }

    private fun scorePad(): View = card {
        addView(text("Score This Ball", 20, GREEN_DARK, true))
        val extraOptions = listOf("" to "Runs", "wd" to "Wide", "nb" to "No ball", "b" to "Bye", "lb" to "Leg bye")
        addView(wrapRow {
            extraOptions.forEach { (value, label) ->
                addView(button(if (selectedExtra == value) "[$label]" else label, if (selectedExtra == value) GREEN else BLUE) {
                    selectedExtra = value
                    renderScorer()
                })
            }
            addView(button(if (wicketMode) "Wicket on" else "Wicket off", RED) {
                wicketMode = !wicketMode
                renderScorer()
            })
        })
        addView(wrapRow {
            listOf(0, 1, 2, 3, 4, 6).forEach { runs ->
                addView(button(runs.toString(), if (runs >= 4) BLUE else GREEN_DARK) {
                    match.scoreBall(runs, selectedExtra, wicketMode)
                    wicketMode = false
                    renderScorer()
                })
            }
        })
    }

    private fun bowlingCard(): View = card {
        addView(text("Bowling", 20, GREEN_DARK, true))
        addView(text("${match.bowler.name}  ${match.oversText(match.bowler.balls)}-${match.bowler.maidens}-${match.bowler.runs}-${match.bowler.wickets}  Econ ${match.bowlerEconomy()}", 16))
    }

    private fun showStats() {
        root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(12), dp(12), dp(24))
            setBackgroundColor(BG)
        }
        setContentView(ScrollView(this).apply { addView(root) })
        root.addView(row {
            addView(titleBlock("Player Stats", "Career database"))
            addView(button("Scorer", GREEN) { showScorer() })
            addView(button("Reset stats", RED) {
                AlertDialog.Builder(this@MainActivity)
                    .setTitle("Reset all stats?")
                    .setMessage("This deletes player stats and match history.")
                    .setPositiveButton("Reset") { _, _ ->
                        db.resetAll()
                        showStats()
                    }
                    .setNegativeButton("Cancel", null)
                    .show()
            })
        })
        val batting = db.players("runs DESC, high_score DESC, name ASC")
        val bowling = db.players("wickets DESC, runs_conceded ASC, name ASC")
        root.addView(listCard("Batting Leaderboard", batting.mapIndexed { i, p ->
            "${i + 1}. ${p.name}  M ${p.matches}  Inns ${p.battingInnings}  Runs ${p.runs}  Avg ${p.battingAverage}  SR ${p.strikeRate}  HS ${p.highScore}  4s ${p.fours}  6s ${p.sixes}"
        }.ifEmpty { listOf("No completed innings yet") }))
        root.addView(listCard("Bowling Leaderboard", bowling.mapIndexed { i, p ->
            "${i + 1}. ${p.name}  M ${p.matches}  Inns ${p.bowlingInnings}  Overs ${p.ballsBowled / 6}.${p.ballsBowled % 6}  Runs ${p.runsConceded}  Wkts ${p.wickets}  Avg ${p.bowlingAverage}  Econ ${p.economy}  Best ${p.bestWickets}/${p.bestRuns}"
        }.ifEmpty { listOf("No completed innings yet") }))
        root.addView(listCard("Match History", db.history().ifEmpty { listOf("No completed matches yet") }))
    }

    private fun promptCard(title: String, hint: String, onSubmit: (String) -> Unit): View = card {
        addView(text(title, 20, BLUE, true))
        val input = edit("", hint)
        addView(input)
        addView(button("Add", GREEN) { onSubmit(input.text.toString()) })
    }

    private fun listCard(title: String, lines: List<String>): View = card {
        addView(text(title, 20, GREEN_DARK, true))
        lines.forEach { addView(text(it, 15)) }
    }

    private fun animateCoinFlip(teamOne: String, teamTwo: String, status: TextView, done: (String) -> Unit) {
        val teams = listOf(teamOne, teamTwo)
        val winner = teams.random()
        val handler = Handler(Looper.getMainLooper())
        var frame = 0
        val frames = 18
        val ticker = object : Runnable {
            override fun run() {
                val face = if (frame % 2 == 0) "HEADS" else "TAILS"
                status.text = "Flipping coin... $face - ${teams[frame % teams.size]}"
                frame += 1
                if (frame <= frames) {
                    handler.postDelayed(this, 90L)
                } else {
                    done(winner)
                }
            }
        }
        handler.post(ticker)
    }

    private fun card(color: Int = SURFACE, block: LinearLayout.() -> Unit): View =
        LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14), dp(14), dp(14), dp(14))
            setBackgroundColor(color)
            val params = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)
            params.setMargins(0, 0, 0, dp(12))
            layoutParams = params
            block()
        }

    private fun row(block: LinearLayout.() -> Unit): View =
        HorizontalScrollView(this).apply {
            addView(LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                block()
            })
        }

    private fun wrapRow(block: LinearLayout.() -> Unit): View =
        LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            block()
        }

    private fun titleBlock(title: String, sub: String): View =
        LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, 0, dp(10), 0)
            addView(text(title, 24, GREEN_DARK, true))
            addView(text(sub, 14))
        }

    private fun text(value: String, sp: Int, color: Int = 0xFF16201B.toInt(), bold: Boolean = false): TextView =
        TextView(this).apply {
            text = value
            textSize = sp.toFloat()
            setTextColor(color)
            setPadding(0, dp(4), 0, dp(4))
            if (bold) typeface = android.graphics.Typeface.DEFAULT_BOLD
        }

    private fun edit(value: String, hintText: String): EditText =
        EditText(this).apply {
            setText(value)
            hint = hintText
            setSingleLine(true)
            setPadding(dp(10), 0, dp(10), 0)
            setBackgroundColor(SOFT)
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(48)).apply {
                setMargins(0, dp(6), 0, dp(6))
            }
        }

    private fun button(label: String, color: Int, onClick: () -> Unit): Button =
        Button(this).apply {
            text = label
            setTextColor(Color.WHITE)
            setBackgroundColor(color)
            setOnClickListener { onClick() }
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, dp(46)).apply {
                setMargins(dp(4), dp(4), dp(4), dp(4))
            }
        }

    private fun toast(message: String) = Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()
}
