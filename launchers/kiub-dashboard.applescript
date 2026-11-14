-- KIUB Dashboard: opens the dashboard page as its own window (no tabs, no address bar) in Brave.
set dashboard to "file:///Users/lude/Desktop/PNT%20-%20OYE/2_KIUB%20/KIUB%202026%20Taff/MEGA_DOC_2026_Taff%20Claude/00_KIUB_DASHBOARD.html"
do shell script "nohup '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser' --app='" & dashboard & "' >/dev/null 2>&1 &"
