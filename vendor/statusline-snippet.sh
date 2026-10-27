# >>> talpy-den >>>
# Snapshot for the Talpy Den widget (plan limits + context window).
# Needs $input to hold the status-line JSON; silent and atomic, and it never
# fails (not even under set -e), so it can't break the status line. Plain
# POSIX sh, so it works in sh (dash), bash and
# zsh scripts. The profile dir comes from the transcript path
# (<cfg>/projects/<project>/<session>.jsonl), else CLAUDE_CONFIG_DIR, links resolved.
# Runs in a subshell so the private umask and variables stay local.
(
  umask 077
  cw_sid='' cw_tp=''
  eval "$(printf '%s' "$input" | jq -r '@sh "cw_sid=\(.session_id // "") cw_tp=\(.transcript_path // "")"')"
  cw_cfg="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  case "$cw_tp" in */projects/*) cw_cfg="${cw_tp%/projects/*}" ;; esac
  cw_cfg="${cw_cfg%/}"
  # A link like ~/.claude can be switched to another profile: file the
  # snapshot under the real folder, so it can't be credited to the wrong one.
  cw_cfg=$(realpath -e -- "$cw_cfg" 2>/dev/null || printf '%s' "$cw_cfg")
  case "$cw_sid" in ''|*[!A-Za-z0-9_-]*) ;; *)
    cw_dir="${XDG_CACHE_HOME:-$HOME/.cache}/talpy-den/statusline/${cw_cfg##*/}"
    mkdir -p "$cw_dir" &&
      printf '%s' "$input" > "$cw_dir/.$cw_sid.tmp" &&
      mv -f "$cw_dir/.$cw_sid.tmp" "$cw_dir/$cw_sid.json"
  ;; esac
) 2>/dev/null || :
# <<< talpy-den <<<
