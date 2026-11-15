-- KIUB Dashboard: opens the dashboard page as its own window in Brave.
-- Brave is started by macOS first (so it keeps its own file permissions), then asked for an app window.
set dashboard to "file:///Users/lude/Desktop/PNT%20-%20OYE/2_KIUB%20/KIUB%202026%20Taff/MEGA_DOC_2026_Taff%20Claude/00_KIUB_DASHBOARD.html"
do shell script "open -g -a 'Brave Browser'; sleep 2; nohup '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser' --app='" & dashboard & "' >/dev/null 2>&1 &"
