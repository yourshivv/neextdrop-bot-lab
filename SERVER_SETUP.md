# Server setup (run on the machine that holds the bot databases)

Goal: every night the server rebuilds the results page and pushes it to GitHub, which serves it as a website.
The server only needs permission to push to THIS one repository, nothing else.

## 1. Give the server a write key for this repo only (a deploy key)

On the server:

    ssh-keygen -t ed25519 -f ~/.ssh/botlab_deploy -N "" -C "botlab-server"
    cat ~/.ssh/botlab_deploy.pub

On GitHub: repo, Settings, Deploy keys, Add deploy key. Paste the line printed above (the .pub file, never the other one).
Tick "Allow write access". Save.

Tell SSH to use that key for this repo. Add to `~/.ssh/config`:

    Host github.com-botlab
      HostName github.com
      User git
      IdentityFile ~/.ssh/botlab_deploy
      IdentitiesOnly yes

Test: `ssh -T git@github.com-botlab` (it should greet you and say it does not provide shell access).

## 2. Clone and configure

    git clone git@github.com-botlab:YOUR_GITHUB_NAME/neextdrop-bot-lab.git ~/neextdrop-bot-lab
    cd ~/neextdrop-bot-lab
    git config user.name "Your Name"
    git config user.email "your-id+yourname@users.noreply.github.com"

Create your PRIVATE settings file (it must stay outside the repo, and `.gitignore` also blocks it):

    mkdir -p ~/.config/neextdrop
    cp results_config.example.json ~/.config/neextdrop/results_config.json
    nano ~/.config/neextdrop/results_config.json

Put the real database path of every bot (files under `user_data/db/`) and add every bot, including failed ones.

## 3. First run

    chmod +x publish.sh
    ./publish.sh

If it prints "Possible secret or server path found", read the line it shows and fix the cause. Do not bypass it.

## 4. Turn on the website (one time, on GitHub)

Repo, Settings, Pages, Source: "Deploy from a branch", Branch: `main`, Folder: `/docs`. Save.
After a minute the site is live at `https://YOUR_GITHUB_NAME.github.io/neextdrop-bot-lab/`.

## 5. Nightly refresh

    crontab -e

Add (runs at 02:15 server time):

    15 2 * * * /home/YOUR_USER/neextdrop-bot-lab/publish.sh >> /home/YOUR_USER/publish.log 2>&1

## Safety rules

- Never put Freqtrade's own web address, passwords, API keys or Telegram details in this repo or in the settings notes.
- Only the `docs/` folder is public output. Look at `docs/index.html` after the first run before telling anyone.
- If the deploy key is ever exposed, delete it on GitHub (Settings, Deploy keys) and make a new one.
