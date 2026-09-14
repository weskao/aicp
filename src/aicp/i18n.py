"""Messages: the mechanism, and the zh-TW catalogue.

Shape copied from ``~/scripts/bin/aicp``'s ``_aicp_t``/``_AICP_ZH`` (itself
copied from ai-accounts' ``i18n.py``): the English text stays at the call
site as ``t()``'s second argument (so the printed wording stays readable
where it's used, and English output never depends on a lookup that could
silently miss); only the zh-TW translation lives in a catalogue dict here. A
missing msgid, or one with no zh-TW entry yet, degrades to the English text
passed in — never a blank line, never a crash.

``AICP_LANG`` is resolved once, at import time (same posture as ai-accounts'
language resolution) — ``en`` or ``zh-TW`` only; anything else is a value the
writer got wrong, so it is announced to stderr and treated as ``en``, never
silently coerced or guessed at.
"""

from __future__ import annotations

import os
import sys

VALID_LANGUAGES = ("en", "zh-TW")
DEFAULT_LANGUAGE = "en"


def _resolve_language() -> str:
    raw = os.environ.get("AICP_LANG", DEFAULT_LANGUAGE)
    if raw not in VALID_LANGUAGES:
        print(
            f"aicp: ignoring AICP_LANG={raw} (expected en or zh-TW) — using en",
            file=sys.stderr,
        )
        return DEFAULT_LANGUAGE
    return raw


# Resolved once, at import — mirrors ai-accounts' i18n posture. A test that
# needs a different language must re-import this module (or call
# `_resolve_language()` directly) after patching the environment.
LANGUAGE = _resolve_language()

# Catalogue: msgid -> zh-TW translation. Wording is carried over from
# bin/aicp's `_AICP_ZH` wherever that script had the same message, so the port
# reads identically to the tool it replaces; only what is new here (the Skills
# and Doctor rows, the installer prompts, `--help`) is freshly written.
#
# House style (from ai-accounts / bin/aicp): fullwidth ：，。（）, a single
# half-width space around embedded Latin/number runs, product and command
# nouns left untranslated (commit, push, branch, remote, git fetch, skill,
# timeout, /safe-git-push), the same emoji per status in both languages, "·"
# as separator, " — " to attach a trailing clause.
#
# tests/test_i18n.py re-derives the msgid set from src/ on every run: an entry
# missing here, an entry nothing uses, and a %s count that drifts from the
# English default are all failures, not silent fallbacks.
CATALOG: dict[str, str] = {
    # ── notifier ────────────────────────────────────────────────────────────
    "telegram_unavailable": "（telegram 無法使用 — 訊息未送出）",
    # ── the run: budget, fallback chain, interruption ───────────────────────
    # bin/aicp carried the ⚠ inside this string; here the call site prints the
    # marker, so it would be doubled.
    "interrupted": "已中斷（Ctrl+C）— 中止執行，後續步驟都不會跑",
    "step_signal_note": "訊號 %s",
    "step_timeout_note": "逾時（超過 %ss）",
    "step_exit_note": "結束碼 %s",
    "budget_hint": "預算 %ss · %s",
    "budget_note_plain": "%s 個檔案 · %s 行",
    "budget_note_history": "%s 個檔案 · %s 行 · 歷史紀錄 %ss",
    "budget_note_fixed": "固定值",
    "budget_note_bad_step": "AICP_STEP_TIMEOUT 值無效 — 改用下限",
    "budget_note_bad_env": "AICP_TIMEOUT_* 值無效 — 改用下限",
    "no_cli_for_step": "  ✗ 沒有任何 AI CLI 能執行這個步驟",
    "no_cli_found": "✗ PATH 上找不到任何 AI CLI",
    "no_cli_looked_for": "  已尋找：%s",
    "no_cli_install": "  請安裝其中一個，或在 %s 設定 AICP_CLI_ORDER",
    # ── secret scan ─────────────────────────────────────────────────────────
    "scan_skipped": "▸ 已略過機密掃描（AICP_SKIP_SECRET_SCAN=1）",
    "scan_binary_warn": "⚠ 機密掃描：%s 個檔案內容為二進位 — 未掃描，請自行確認：",
    "scan_found": "✗ 機密掃描發現 %s 筆疑似機密 — 在 /commit 前中止，未呼叫任何 AI CLI",
    "scan_advise": "  請移除或遮蔽該字串後重跑；若為誤判，可用 AICP_SKIP_SECRET_SCAN=1 單次略過。",
    # ── git preconditions and --undo ────────────────────────────────────────
    "not_on_branch": "✗ 不在任何分支上（detached HEAD，或不是 git repo）",
    "undo_no_parent": "✗ 沒有可還原的 commit — HEAD 沒有上一層 commit",
    "undo_unverifiable": "✗ 拒絕 --undo：無法驗證 %s/%s（不可達，或從未推送過）— 有可能正在改寫已推送的歷史",
    "undo_already_pushed": "✗ 拒絕 --undo：最後一個 commit 已經在 %s/%s 上 — undo 會改寫已推送的歷史",
    "undo_reset_failed": "✗ git reset --soft HEAD^ 失敗",
    "undo_done": "✓ 已還原：",
    "undo_note": "  變更已回到 index — 沒有遺失，也沒有推送出去。",
    # ── --swap-ai ───────────────────────────────────────────────────────────
    "swap_current_order": "目前的 fallback 順序：",
    "swap_current_first": "  ← 目前的 #1",
    "swap_prompt": "請選擇要移到 #1 的 CLI（1-%s）：",
    "swap_invalid": "✗ 無效的選擇：%s",
    "swap_already_first": "▸ %s 已經是 #1 — 不需變更",
    "swap_new_order": "✓ 新的順序：",
    "swap_saved": "  已儲存到 %s — 下一次 aicp 就會採用",
    "persist_failed": "✗ 寫入 %s 失敗",
    # ── the flow: spinners and the two step-skip notices ────────────────────
    "spin_precheck": "正在檢查工作目錄…",
    "spin_fetch": "正在 git fetch %s/%s…",
    "active_chain": "▸ 順序：%s",
    "skip_commit_clean": "▸ 沒有未提交的變更 — 略過 /commit",
    "skip_commit_config": "▸ /commit 已在設定中關閉（AICP_DO_COMMIT=0）— 略過",
    "skip_push_synced": "▸ %s/%s 已經同步 — 略過 /safe-git-push",
    "skip_push_config": "▸ /safe-git-push 已在設定中關閉（AICP_DO_PUSH=0）— 略過",
    # ── the RESULT table ────────────────────────────────────────────────────
    "fetch_note": "⚠ git fetch %s %s 失敗 — 下方的 Ahead/Behind 讀自上一次已知的遠端狀態，不是剛更新的",
    "state_in_sync": "✅ 已同步",
    "state_not_pushed": "⚠️  未推送",
    "state_remote_missing": "✗ 找不到 %s/%s",
    "result_title": "結果",
    "result_repo": "Repo",
    "result_branch": "分支",
    "result_remote": "遠端",
    "result_new_commits": "新增 commit",
    "result_ahead": "領先",
    "result_behind": "落後",
    "result_status": "狀態",
    "commit_handler": "Commit 處理 CLI",
    "push_handler": "Push 處理 CLI",
    "panel_value_header": "值",
    "panel_repo": "repo",
    "panel_branch": "分支",
    "commits_panel_title": "新增的 COMMIT",
    "cannot_verify": "無法驗證這次 push：%s/%s 解析不出來。",
    "cannot_verify_note": "  遠端可能不可達，或這個分支從未推送過。",
    "local_not_pushed": "本地未推送：",
    "remote_not_merged": "遠端未合入：",
    # ── Telegram ────────────────────────────────────────────────────────────
    "tg_timeout": "⏱️ aicp：%s 執行「%s」時 %s 逾時（超過 %ss 預算，%s），已切換下一個 CLI。",
    "step_quota_note": "配額／速率限制已耗盡 — 本次執行略過",
    "step_quota_cooling": "配額冷卻中 — 尚餘 %s 分鐘，略過",
    "tg_quota": "⚠️ aicp：%s 執行「%s」時 %s 的配額／速率限制已耗盡 — 本次執行後續略過。",
    "usage_limit_reached": "usage_limit_reached",
    "tg_unverifiable": "⚠️ aicp：分支 %s（%s）無法驗證 push —— %s/%s 無法解析（遠端不可達，或這個分支從未推送過）。新增 commit %s 個。",
    "tg_not_synced": "⚠️ aicp：分支 %s（%s）未與 %s/%s 同步",
    "tg_tbl_item": "項目",
    "tg_tbl_value": "值",
    # ── --config menu: rows, help lines, the typed-choice surface ───────────
    "config_title": "aicp 設定",
    "config_group_steps": "執行階段",
    "config_group_general": "一般",
    "config_group_tools": "工具",
    "config_do_commit": "執行 /commit 階段",
    "config_do_push": "執行 /safe-git-push 階段",
    "config_language": "語言",
    "config_cli_order": "AI CLI 順序",
    # The feature is called Skills in every AI CLI that has it; translating it
    # would only make the row harder to match to what the vendors call it.
    "config_skills": "Skills",
    "config_doctor": "健康檢查",
    "config_on": "開啟",
    "config_off": "關閉",
    "config_help_commit": "關閉時不 stage 也不 commit，只推送已經 commit 的內容。",
    "config_help_push": "關閉時只做 commit 就停下，分支會領先遠端。",
    "config_help_lang": "所有訊息的語言，包含 Telegram 通知。",
    "config_help_cli": "完整的 fallback 順序，由左到右依序嘗試；←/→ 可輪替順序，未安裝的 CLI 會自動跳過。",
    "config_help_skills": "把 aicp 的 /commit 與 /safe-git-push 裝進每個 AI CLI，你自己的同名檔案一律保留。",
    "config_help_doctor": "平常不會出聲的項目：timeout、被略過的 config.json 設定、skills、git remote。",
    "config_keys_plain": "1-%s 切換 · q 離開 · 自動儲存",
    "config_keys_tui": "↑↓ 選擇 · ←→ 切換 · ⏎ 切換／執行 · q/Ctrl-C 離開 · 自動儲存",
    "config_prompt": "請選擇要修改的項目（1-%s，q 離開）：",
    "config_bad_number": "⚠ 請輸入上面列出的項目編號。",
    # ── Skills: the row's inline status, the report, the installer prompts ──
    "skills_none": "沒有已設定的 CLI",
    "skills_missing": "⚠ %s 缺少",
    "skills_older": "⚠ %s 需更新",
    "skills_yours": "✓ 保留你自己的",
    "skills_ok": "✓ 已安裝",
    "skills_title": "SKILLS",
    "skills_skipped": "這裡沒有安裝：%s",
    "skills_state_missing": "尚未安裝",
    "skills_state_current": "已安裝（%s）",
    "skills_state_older": "來自舊版 aicp（%s）— 可以更新",
    "skills_state_foreign": "你自己的檔案 — aicp 會直接沿用",
    "skills_install_q": "要安裝／更新 %s 個 skill 嗎？[y/N]：",
    "skills_keep_note": "保留你自己的 %s 個 skill — aicp 會直接沿用，這不是錯誤。",
    "skills_force_q": "要改用 aicp 的版本嗎？你的檔案會先備份 [y/N]：",
    "skills_installed": "%s 已安裝到 %s",
    "skills_upgraded": "%s 已為 %s 更新",
    "skills_overwritten": "%s 已為 %s 換成 aicp 的版本 — 你的原檔在 %s",
    "skills_kept": "%s：保留你自己的版本 — aicp 會直接沿用",
    "skills_nothing": "沒有需要處理的項目。",
    "install_needs_yes": "  要實際寫入請加上 --install-skills --yes",
    "skill_nudge": "▸ %s 缺少 %s skill — 執行 aicp --config 安裝",
    # ── Doctor: the row's inline status and the health report ───────────────
    "doctor_clear": "✓ 一切正常",
    "doctor_one": "⚠ 1 項提醒",
    "doctor_many": "⚠ %s 項提醒",
    "health_footer": "提醒只是讓你知道，不是失敗 — aicp 照樣可以跑。",
    "health_timeout_ok": "每次 CLI 呼叫的 timeout 用 %s",
    "health_timeout_missing": "PATH 上沒有 timeout／gtimeout — 每次 AI CLI 呼叫都沒有時間上限",
    "health_no_config": "還沒有 %s — 全部採用內建預設值",
    "health_config_ok": "%s：讀到 %s 個設定",
    "health_env_only": "%s 在 %s 裡會被忽略 — 這個項目只能從環境變數／PATH 取得",
    "health_dropped": "%s 被 %s 略過 — 值裡有不允許的字元",
    "health_skills_none": "找不到任何 AI CLI 的設定目錄 — 沒有地方可以安裝 skills",
    "health_skill_missing": "%s：%s skill 尚未安裝 — 用 Skills 那一列安裝",
    "health_skill_older": "%s：%s skill 來自舊版 aicp — 用 Skills 那一列更新",
    "health_skill_foreign": "%s：%s skill 是你自己的 — aicp 會直接沿用",
    "health_skills_ok": "所有已設定的 CLI 都有 skills",
    "health_no_branch": "目前不在分支上（detached HEAD，或不是 git repo）— aicp 需要分支才能 push",
    "health_no_remote": "這個 repo 沒有 git remote — 可以 commit，但沒有地方 push",
    "health_git_ok": "分支 %s → remote %s",
    # ── --help (argparse's own furniture stays English; see cli.py) ─────────
    "help_description": "AI commit + push，附一份 git 驗證過的結果摘要。",
    "help_verbose": "即時輸出每個 CLI 的內容，不用轉圈動畫",
    "help_undo": "還原最後一個 commit（git reset --soft HEAD^）；已經在遠端上就拒絕。不呼叫任何 AI CLI",
    "help_swap_ai": "把某個 CLI 移到 fallback 順序的 #1，並存進 config.json",
    "help_config": "設定選單：兩個執行階段、語言、CLI 順序",
    "help_doctor": "列出每個 CLI 的內建 skill 各自處於什麼狀態",
    "help_json": "--doctor 改輸出 JSON",
    "help_install_skills": "把內建的 skills 安裝到每個已設定的 CLI",
    "help_yes": "--install-skills：實際寫入（不加就只是預覽）",
    "help_force": "--install-skills：覆蓋別人的同名檔案，並留下 .bak",
    "help_epilog": "以上任一 flag 都可以省略開頭的 `--`（`aicp config` 等同 `aicp --config`）。",
}


def t(msgid: str, english: str, *args: object) -> str:
    """Look up *msgid*; zh-TW translation if resolved and present, else
    *english*. With no *args* the text is returned verbatim (never run
    through ``%``-formatting), so a literal ``%`` in a message can never be
    misread as a format specifier.
    """
    text = CATALOG.get(msgid, english) if LANGUAGE != "en" else english
    if args:
        return text % args
    return text
