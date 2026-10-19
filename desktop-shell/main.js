const { app, BrowserWindow, shell } = require('electron')
const { spawn } = require('child_process')
const path = require('path')

const projectRoot = path.resolve(__dirname, '..')
const startScript = path.join(projectRoot, 'start_services.py')
let mainWindow = null
let bootProcess = null

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 960,
    minWidth: 1200,
    minHeight: 800,
    webPreferences: {
      contextIsolation: true,
      sandbox: true
    }
  })

  mainWindow.loadURL('http://127.0.0.1:15173')
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })
}

function bootServices() {
  const python = process.platform === 'win32' ? 'python' : 'python3'
  bootProcess = spawn(python, [startScript], {
    cwd: projectRoot,
    detached: false,
    stdio: 'ignore'
  })
}

app.whenReady().then(() => {
  bootServices()
  setTimeout(createWindow, 6000)
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

