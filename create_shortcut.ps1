# Cria um atalho "AI Usage Bar.lnk" na área de trabalho, apontando pro
# pythonw.exe (sem console) rodando widget.py desta mesma pasta.
#
# Uso: abra o PowerShell DENTRO da pasta do projeto e rode:
#   powershell -ExecutionPolicy Bypass -File create_shortcut.ps1

$pythonwCmd = Get-Command pythonw -ErrorAction SilentlyContinue
if (-not $pythonwCmd) {
    Write-Error "pythonw.exe não encontrado no PATH. Instale o Python (python.org) e tente de novo."
    exit 1
}
$pythonw = $pythonwCmd.Source
$script = Join-Path $PSScriptRoot "widget.py"
$desktop = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "AI Usage Bar.lnk"

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($linkPath)
$Shortcut.TargetPath = $pythonw
$Shortcut.Arguments = "`"$script`""
$Shortcut.WorkingDirectory = $PSScriptRoot
$Shortcut.IconLocation = $pythonw
$Shortcut.Description = "AI Usage Bar - uso de Claude/DeepSeek/etc"
$Shortcut.Save()

Write-Output "Atalho criado em: $linkPath"
Write-Output "Dica: pra abrir sozinho junto com o Windows, copie esse .lnk pra pasta de Inicialização (Win+R -> shell:startup)."
