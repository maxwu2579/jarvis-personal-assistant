# 用 Microsoft Word 渲染检查生成的 DOCX：
# 1) 打开文档；2) 更新目录域与所有域；3) 统计页数；4) 导出 PDF；5) 保存。
#
# 用法: powershell -File scripts/docs/word_render_check.ps1
# 前置: 本机安装了 Microsoft Word（COM 自动化）。

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
$docDir = Join-Path $projectRoot "docs\learning"
$files = Get-ChildItem -Path $docDir -Filter "*.docx" | Sort-Object Name

$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0

try {
    foreach ($f in $files) {
        $doc = $word.Documents.Open($f.FullName, $false, $true)  # ReadOnly 打开，检查后另存 PDF
        if ($doc.TablesOfContents.Count -gt 0) {
            $doc.TablesOfContents.Item(1).Update() | Out-Null
        }
        $doc.Repaginate() | Out-Null
        $pages = $doc.ComputeStatistics(2)  # wdStatisticPages
        $pdfPath = [System.IO.Path]::ChangeExtension($f.FullName, ".pdf")
        $doc.SaveAs2($pdfPath, 17)          # wdFormatPDF = 17
        $doc.Close($false)
        Write-Output ("{0} | pages={1} | pdf={2}" -f $f.Name, $pages, $pdfPath)
    }
} finally {
    $word.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($word) | Out-Null
}
