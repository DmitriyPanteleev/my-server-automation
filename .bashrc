#!/bin/bash

# If not running interactively, don't do anything
case $- in
    *i*) ;;
      *) return;;
esac

[ -n "$SSH_TTY" ] && [ "${BASH_SOURCE[0]}" == "${0}" ] && exec bash --rcfile "$SHELL" "$@"

[ -z "$PS1" ] && return

((SHLVL == 1)) && [ -r /etc/profile ] && . /etc/profile
[ -r /etc/skel/.bashrc ] && . <(grep -v "^HIST.*SIZE=" /etc/skel/.bashrc)
[ -d "$HOME/bin" ] && [[ ":$PATH:" != *":$HOME/bin:"* ]] && PATH="$HOME/bin:$PATH"

[ -z "$SSH_TTY" ] && command -v socat >/dev/null && {
    history_port=26574
    netstat -lnt|grep -q ":${history_port}\b" || {
        umask 077 && socat -u TCP4-LISTEN:$history_port,bind=127.0.0.1,reuseaddr,fork OPEN:$HOME/.bash_eternal_history,creat,append &
    }
}

# History parameter.
HISTSIZE=$((2048 * 2048))
HISTFILESIZE=$HISTSIZE
HISTTIMEFORMAT='%t%F %T%t'
HISTCONTROL=ignoreboth

# append to the history file, don't overwrite it
# update the values of LINES and COLUMNS.
shopt -s histappend
shopt -s checkwinsize

# make less more friendly for non-text input files, see lesspipe(1)
[ -x /usr/bin/lesspipe ] && eval "$(SHELL=/bin/sh lesspipe)"

# set variable identifying the chroot you work in (used in the prompt below)
if [ -z "${debian_chroot:-}" ] && [ -r /etc/debian_chroot ]; then
    debian_chroot=$(cat /etc/debian_chroot)
fi

force_color_prompt=yes

if [ -n "$force_color_prompt" ]; then
    if [ -x /usr/bin/tput ] && tput setaf 1 >&/dev/null; then
        # We have color support; assume it's compliant with Ecma-48
        # (ISO/IEC-6429). (Lack of such support is extremely rare, and such
        # a case would tend to support setf rather than setaf.)
        export color_prompt=yes
    else
        export color_prompt=
    fi
fi

update_eternal_history() {
    local histfile_size=$(umask 077 && touch $HISTFILE && stat -c %s $HISTFILE)
    history -a
    ((histfile_size == $(stat -c %s $HISTFILE))) && return
    local history_line="${USER}\t${HOSTNAME}\t${PWD}\t$(history 1)"
    local history_sink=$(readlink ~/.bash-ssh.history 2>/dev/null)
    [ -n "$history_sink" ] && echo -e "$history_line" >"$history_sink" 2>/dev/null && return
    local old_umask=$(umask)
    umask 077
    echo -e "$history_line" >> ~/.bash_eternal_history
    umask $old_umask
}

[[ "$PROMPT_COMMAND" == *update_eternal_history* ]] || PROMPT_COMMAND="update_eternal_history;$PROMPT_COMMAND"

# Alias definitions.
# You may want to put all your additions into a separate file like
# ~/.bash_aliases, instead of adding them here directly.
# See /usr/share/doc/bash-doc/examples in the bash-doc package.

# Add local aliases if local config exists
if [ -f ~/.bash_aliases ]; then
    . ~/.bash_aliases
fi

# Common usefull aliases
alias ls='ls --color=auto'
alias grep='grep --color=auto'
alias bcat='batcat --paging=never'
alias hists='history | cut -f3-'
alias veros='cat /etc/*rel*'
alias outip='curl -s ipinfo.io | jq -r .ip'
alias totalclean='sudo find /var/log/ -type f -regex ".*log\.[1-9].*" -delete && sudo find /var/log/atop -type f -mtime +0 -delete && sudo find /var/log/container -type f -mtime +30 -delete && sudo journalctl --rotate && sudo journalctl --vacuum-time=1s && sudo docker system prune -af --volumes'

# Custom ssh function
sssh() {
    local ssh="ssh -R 22422:localhost:22422 -S ~/.ssh/control-socket-$(tr -cd '[:alnum:]' < /dev/urandom|head -c8)"
    local bashrc=~/.bashrc
    local history_command="rm -f ~/.bash-ssh.history"
    [ -r ~/.bash-ssh ] && bashrc=~/.bash-ssh && history_port=$(basename $(readlink ~/.bash-ssh.history 2>/dev/null))
    $ssh -fNM "$@" || return $?
    [ -n "$history_port" ] && {
        local history_remote_port="$($ssh -O forward -R 0:127.0.0.1:$history_port placeholder)"
        history_command="ln -nsf /dev/tcp/127.0.0.1/$history_remote_port ~/.bash-ssh.history"
    }
    $ssh placeholder "${history_command}; cat >~/.bash-ssh" < $bashrc
    $ssh "$@" -t 'SHELL=~/.bash-ssh; chmod +x $SHELL; exec bash --rcfile $SHELL -i'
    $ssh placeholder -O exit >/dev/null 2>&1
}

# Copy form workstation to server function
copytohere() {
    local target=$1
    local destination=${2:-.}

    # Generate correct target
    if [[ $target != /* ]]; then
        target="/home/dpanteleev/$target"
    fi

    # Generate correct destination
    if [[ $destination != /* ]]; then
        destination="$(pwd)/$destination"
    fi

    # Copy file
    scp -P 22422 "localhost:$target" "$destination"
}

# Copy form server to workstation function
copyfromhere() {
    local target=$1
    local destination=${2:-.}

    # Generate correct target
    if [[ $target != /* ]]; then
        target="$(pwd)/$target"
    fi

    # Generate correct destination
    if [[ $destination != /* ]]; then
        destination="/home/dpanteleev/$destination"
    fi

    # Copy file
    scp -P 22422 "$target" "localhost:$destination"
}

# Disk usage function
dusage() {
    sudo du -ah --max-depth=1 "$1" | sort -rh | head -n 10
}

# Docker log function
dockerlog() {
    sudo journalctl -n 50 -f -u docker-"$1"
}

# Find killed docker containers function
dockerkilled(){
    sudo docker ps >docker.tmp; sudo cat /var/log/kern.log | grep -i "killed as"  | awk '{print $1,$7}' | while read T DOCK; do T=$(echo $T|sed "s/\..*$//"); DOCK=${DOCK#/*/}; DOCK=${DOCK:0:7}; DOCKR=$(cat docker.tmp|grep $DOCK|tail -n 1| awk '{print $NF}'); if [ -z "$DOCKR" ]; then DOCKR="$DOCK"; fi; echo "${T} $DOCKR"; done | tee killed.txt
}

# Docker ps function
dockerps() {
    if [ -z "$1" ]
    then
        sudo docker ps --format="table{{.Names}}\t{{.Status}}\t{{.Image}}"
    else
        sudo docker ps --format="table{{.Names}}\t{{.Status}}\t{{.Image}}" | grep "$1"
    fi
}

# Docker ports function
dockerports() {
    echo -e "\033[33mContainer $1 ports: \033[0m"
    sudo docker exec -it "$1" bash --login -c 'env | grep -i port'
}

# Local ips function
locip() {
    ip -h a | grep inet | awk '{print $2}' | cut -d '/' -f 1 | grep -vE '127\.0\.0\.1|::1'
}

# IP info function
function ipinfo() {
  curl -s ipinfo.io | jq .
}

# AWS type function
function awstype() {
  curl -s http://169.254.169.254/latest/meta-data/instance-type && echo
}

# Switch to log container directory function
function logdir() {
cd "/var/log/container/$1" || exit
}

# Finder function
function ffind() {
  sudo find "$2" -iname "*$1*"
}

# enable programmable completion features (you don't need to enable
# this, if it's already enabled in /etc/bash.bashrc and /etc/profile
# sources /etc/bash.bashrc).
if ! shopt -oq posix; then
  if [ -f /usr/share/bash-completion/bash_completion ]; then
    . /usr/share/bash-completion/bash_completion
  elif [ -f /etc/bash_completion ]; then
    . /etc/bash_completion
  fi
fi

host_environment=$(cat /etc/ansible/facts.d/main.fact | jq -r .host_environment)

case "$host_environment" in
    production) PS1="\[\e[1;31m\]┌──\[\e[1;31m\][ \[\e[35m\]\$(cat /proc/loadavg | cut -d' ' -f 1-3) $(grep 'processor' /proc/cpuinfo | wc -l)C $(free -m | awk 'FNR==2{printf "%d", $7}')/$(free -m | awk 'FNR==2{printf "%d", $2}')MB\[\e[1;31m\] ] [ \[\e[36m\]\$(date)\[\e[1;31m\] ]\n├──[ \[\e[1;34m\]\u\[\e[1;31m\]@\[\e[1;37m\]\h \[\e[1;31m\]] [ \[\e[36m\]\w\[\e[1;31m\] ]\n└> \[\e[1;35m\]~\[\e[0m\] " ;; # red
    staging) PS1="\[\e[1;33m\]┌──\[\e[1;33m\][ \[\e[35m\]\$(cat /proc/loadavg | cut -d' ' -f 1-3) $(grep 'processor' /proc/cpuinfo | wc -l)C $(free -m | awk 'FNR==2{printf "%d", $7}')/$(free -m | awk 'FNR==2{printf "%d", $2}')MB\[\e[1;33m\] ] [ \[\e[36m\]\$(date)\[\e[1;33m\] ]\n├──[ \[\e[1;34m\]\u\[\e[1;33m\]@\[\e[1;37m\]\h \[\e[1;33m\]] [ \[\e[36m\]\w\[\e[1;33m\] ]\n└> \[\e[1;35m\]~\[\e[0m\] " ;; # yellow
    *) PS1="\[\e[0m\]┌──\[\e[0m\][ \[\e[35m\]\$(cat /proc/loadavg | cut -d' ' -f 1-3) $(grep 'processor' /proc/cpuinfo | wc -l)C $(free -m | awk 'FNR==2{printf "%d", $7}')/$(free -m | awk 'FNR==2{printf "%d", $2}')MB\[\e[0m\] ] [ \[\e[36m\]\$(date)\[\e[0m\] ]\n├──[ \[\e[1;34m\]\u\[\e[0m\]@\[\e[1;37m\]\h \[\e[0m\]] [ \[\e[36m\]\w\[\e[0m\] ]\n└> \[\e[1;35m\]~\[\e[0m\] " ;; # default
esac
