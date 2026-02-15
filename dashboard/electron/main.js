const { app, BrowserWindow } = require("electron");

const NEXT_URL = "http://localhost:3000";

const LOADING_HTML = `data:text/html,
<html>
<body style="margin:0;background:#111827;color:#9ca3af;display:flex;align-items:center;justify-content:center;height:100vh;font-family:system-ui">
  <div style="text-align:center">
    <h2 style="color:#e5e7eb;margin-bottom:8px">DBVictory Bot</h2>
    <p id="msg">Starting dashboard...</p>
  </div>
  <script>
    let dots = 0;
    setInterval(() => {
      dots = (dots + 1) % 4;
      document.getElementById("msg").textContent = "Starting dashboard" + ".".repeat(dots);
    }, 400);
  </script>
</body>
</html>`;

function createWindow() {
  const win = new BrowserWindow({
    width: 820,
    height: 640,
    title: "DBVictory Bot",
    autoHideMenuBar: true,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  // Show loading screen immediately
  win.loadURL(LOADING_HTML);

  // Poll until Next.js is ready, then load
  const tryLoad = () => {
    fetch(NEXT_URL)
      .then((res) => {
        if (res.ok) win.loadURL(NEXT_URL);
        else setTimeout(tryLoad, 500);
      })
      .catch(() => setTimeout(tryLoad, 500));
  };
  tryLoad();
}

app.whenReady().then(createWindow);

app.on("window-all-closed", () => {
  app.quit();
});
