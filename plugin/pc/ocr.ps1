# ocr.ps1 - Windows native OCR that returns text WITH boxes, as JSON.
#
# ASCII only (BOM-less .ps1 is parsed as ANSI by Windows PowerShell). The search
# word arrives as an argument, never as a literal in this file.
#
#   ocr.ps1 <image.png> [findText]
#
# Output: one JSON object on stdout:
#   {"ok":true,"lang":"zh-Hans-CN","image":"...","lines":["..."],
#    "words":[{"t":"发送","x":1410,"y":975,"w":60,"h":28}],"hit":{"t":"...",...}|null}
#
# Word boxes are in IMAGE pixels, so a full-screen grab maps 1:1 to screen
# coordinates; a window grab must be offset by the window origin in the caller.

param(
    [Parameter(Mandatory = $true)][string]$Image,
    [string]$Find = "",
    [string]$OutFile = ""
)

$ErrorActionPreference = 'Stop'

function Emit($obj) {
    $json = $obj | ConvertTo-Json -Depth 6 -Compress
    if ($OutFile -ne "") {
        # 写文件而不是靠控制台：中文/反斜杠/控制字符都可能被管道改掉
        [System.IO.File]::WriteAllText($OutFile, $json, (New-Object System.Text.UTF8Encoding($false)))
        Write-Output ("ok " + $OutFile)
    } else {
        Write-Output $json
    }
    exit 0
}

try {
    $candidates = @(
        (Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\System.Runtime.WindowsRuntime.dll'),
        (Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\System.Runtime.WindowsRuntime.dll')
    )
    $loaded = $false
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) {
            try { Add-Type -Path $c -ErrorAction Stop; $loaded = $true; break } catch { }
        }
    }
    if (-not $loaded) { throw 'cannot load System.Runtime.WindowsRuntime' }

    function Await($op, $resultType) {
        $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
            $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        })[0]
        $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($op))
        $task.Wait(30000) | Out-Null
        return $task.Result
    }

    [void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    [void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
    [void][Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
    [void][Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]

    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Image)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

    # Pick an engine in this order:
    #   1. user profile language (what this machine is set to)
    #   2. Chinese, then Japanese/Korean, then English - the packs people
    #      actually install when they work with CJK content
    #   3. whatever language pack is installed (first one available)
    # A machine with only en-US installed used to fail outright; now it uses
    # English OCR instead of returning nothing.
    $engine = $null
    try { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() } catch { }
    if ($null -eq $engine) {
        foreach ($tag in @('zh-Hans-CN', 'zh-Hans', 'zh-CN', 'ja-JP', 'ko-KR', 'en-US')) {
            try {
                $lang = New-Object Windows.Globalization.Language $tag
                $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
                if ($null -ne $engine) { break }
            } catch { }
        }
    }
    if ($null -eq $engine) {
        try {
            $avail = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages
            if ($avail.Count -gt 0) {
                $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($avail[0])
            }
        } catch { }
    }
    if ($null -eq $engine) {
        throw 'no OCR language pack installed (Settings > Time & Language > Language > add a language)'
    }

    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

    $lines = New-Object System.Collections.ArrayList
    $words = New-Object System.Collections.ArrayList
    $hit = $null
    foreach ($l in $result.Lines) {
        [void]$lines.Add($l.Text)
        foreach ($wd in $l.Words) {
            $r = $wd.BoundingRect
            $item = [pscustomobject]@{
                t = $wd.Text
                x = [int]$r.X; y = [int]$r.Y
                w = [int]$r.Width; h = [int]$r.Height
            }
            [void]$words.Add($item)
            if ($Find -ne "" -and $null -eq $hit -and $wd.Text -like "*$Find*") { $hit = $item }
        }
        if ($Find -ne "" -and $null -eq $hit -and $l.Text -like "*$Find*") {
            $fr = $l.Words[0].BoundingRect
            $hit = [pscustomobject]@{ t = $l.Text; x = [int]$fr.X; y = [int]$fr.Y; w = 0; h = 0 }
        }
    }

    Emit ([pscustomobject]@{
            ok = $true
            lang = $engine.RecognizerLanguage.LanguageTag
            image = $Image
            lines = @($lines)
            words = @($words)
            hit = $hit
        })
}
catch {
    Emit ([pscustomobject]@{
            ok = $false
            error = $_.Exception.Message
            inner = $(if ($_.Exception.InnerException) { $_.Exception.InnerException.Message } else { "" })
        })
}
