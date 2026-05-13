@echo off
cd /d "%~dp0"
if not exist ".git" (
    git init
    git remote add origin https://github.com/higgiAPPMASTER/MPA-Hub.git
)
git add -A
git commit -m "Update MPA Hub"
git branch -M main
git push origin main --force
echo Done! Check Render dashboard.
pause
