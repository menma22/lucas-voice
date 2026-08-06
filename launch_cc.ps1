# Lucas-CC 起動ランチャー
# なぜこのファイルが要るか: wt.exe はコマンドラインの「;」を自身の区切り文字として
# 解釈するため、「Set-Location ...; claude」を直接渡すと分割事故が起きる
# （エラー 0x80070002。2026-07-11 実発生）。セミコロンをこのスクリプトに封じ込める。
# ⚠このファイルは必ず BOM付きUTF-8 で保存すること。BOMなしだと PowerShell 5.1 が
# cp932 として読み、日本語コメントが改行を食って次の行のコードを飲み込む（実発生済み）。
# ワークスペースは lucas-voice 固定（まひろ指示 2026-07-11）。引数が無くても固定先へ。
param([string]$WorkDir = 'C:\Users\mahim\lucas-voice')

Set-Location -LiteralPath $WorkDir

# 起動直前の実 cwd を証跡として残す（cwd バグの再発検知用）
(Get-Location).Path | Out-File -FilePath (Join-Path $PSScriptRoot 'logs\last_cc_cwd.txt') -Encoding utf8

# JARVIS persona is injected ONLY on the wake-word path via this launcher.
# The nightly interview starts through a different launcher (launch-interview.ps1),
# so it stays as the normal casual Lucas. Keep this comment ASCII-only to avoid
# any BOM/cp932 garbling of the launch line.
claude --append-system-prompt-file "C:\Users\mahim\lucas-voice\jarvis-persona.md"
