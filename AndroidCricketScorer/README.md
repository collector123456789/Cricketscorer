# Cricket Scorer Android

Native Kotlin version of the Flask cricket scorer.

## Open in Android Studio

1. Open the `AndroidCricketScorer` folder in Android Studio.
2. Let Android Studio sync Gradle.
3. Run the `app` configuration on an emulator or Android phone.

## Main files

- `app/src/main/java/com/example/cricketscorer/MainActivity.kt`
  - Match setup
  - Ball-by-ball scoring
  - Extras, wickets, retirements, strike rotation
  - Innings transition and match result
  - SQLite player stats and match history
- `app/src/main/AndroidManifest.xml`
- `app/build.gradle`

This project uses a native Android UI built in Kotlin code and Android's built-in SQLite APIs.
