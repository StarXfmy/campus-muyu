$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ws = New-Object -ComObject WScript.Shell

function New-Launcher($name, $command, $desc) {
    $path = Join-Path $root $name
    if (Test-Path $path) { Remove-Item $path -Force }
    $s = $ws.CreateShortcut($path)
    $s.TargetPath = "$env:SystemRoot\system32\cmd.exe"
    $s.Arguments = "/k chcp 65001 >nul && $command"
    $s.WorkingDirectory = $root
    $s.Description = $desc
    $s.IconLocation = "$env:SystemRoot\system32\cmd.exe,0"
    $s.Save()
    Write-Output "OK: $name"
}

New-Launcher '★1 点我聊天.lnk' "py -3 `"$root\scripts\chat.py`"" '命令行里跟校园助手聊天'
New-Launcher '★2 启动飞书机器人.lnk' "py -3 `"$root\app\feishu\bot.py`"" '启动飞书校园助手机器人'
New-Launcher '★3 重新授权.lnk' "py -3 `"$root\scripts\reauth.py`"" '补二课活动和信息流权限'

Write-Output '--- verify ---'
foreach ($f in @('★1 点我聊天.lnk', '★2 启动飞书机器人.lnk', '★3 重新授权.lnk')) {
    $s = $ws.CreateShortcut((Join-Path $root $f))
    Write-Output "args=$($s.Arguments)"
}
