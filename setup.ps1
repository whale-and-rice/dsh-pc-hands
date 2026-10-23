# setup.ps1 - one-command installer for dsh-pc-hands.
#
# ASCII only: a BOM-less .ps1 is parsed as ANSI by Windows PowerShell, so any
# non-ASCII byte here would be mangled on a non-Chinese machine.
#
#   .\setup.cmd                 install (scripts + deps), no profile changes
#   .\setup.cmd -WithOCR        also install RapidOCR (better accuracy, ~100MB)
#   .\setup.cmd -InstallPlugin  also register the plugin in the DSH profile
#   .\setup.cmd -Check          only report what is / is not ready
#
# Nothing here needs admin rights.

param(
    [string]$PcDir = "",
    [switch]$WithOCR,
    [switch]$InstallPlugin,
    [switch]$Check
)

# 'Continue', not 'Stop': native programs (pip, python) write warnings to stderr,
# and 'Stop' would turn those into terminating errors that abort the setup.
$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$problems = @()
$notes = @()

function Say($text) { Write-Host $text }
function Ok($text) { Write-Host ("  OK    " + $text) -ForegroundColor Green }
function Warn($text) { Write-Host ("  WARN  " + $text) -ForegroundColor Yellow }
function Fail($text) { Write-Host ("  FAIL  " + $text) -ForegroundColor Red }

function Resolve-Python {
    $cands = @()
    foreach ($name in @('py', 'python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $cands += , @($name, @('-c', 'import sys;print(sys.executable)')) }
    }
    # py -3 must be asked for version 3 explicitly
    $cands = @(
        @('py', @('-3', '-c', 'import sys;print(sys.executable)')),
        @('python', @('-c', 'import sys;print(sys.executable)')),
        @('python3', @('-c', 'import sys;print(sys.executable)'))
    )
    foreach ($c in $cands) {
        try {
            $out = & $c[0] $c[1] 2>&1
            if ($LASTEXITCODE -eq 0 -and $out) {
                $p = ($out | Select-Object -First 1).Trim()
                if (Test-Path -LiteralPath $p) { return $p }
            }
        } catch { }
    }
    foreach ($p in @(
            (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
            'C:\Python312\python.exe',
            'C:\Python311\python.exe')) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}

function Get-PythonVersion($python) {
    try {
        $v = & $python -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>&1
        return ($v | Select-Object -First 1).Trim()
    } catch { return "" }
}

function Test-Module($python, $module) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $null = & $python -c "import $module" 2>&1
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
    finally { $ErrorActionPreference = $prev }
}

Say ""
Say "dsh-pc-hands setup"
Say "-------------------"

# ---- 1. python ----
$python = Resolve-Python
if (-not $python) {
    Fail "Python not found."
    Say "        Install Python 3.9+ from https://www.python.org/downloads/windows/"
    Say "        (tick 'Add python.exe to PATH' during install), then run setup again."
    exit 2
}
$ver = Get-PythonVersion $python
Ok ("Python " + $ver + "  " + $python)
$notes += ("python=" + $python)

# ---- 2. pillow ----
if (Test-Module $python 'PIL') {
    Ok "Pillow present"
} elseif ($Check) {
    Fail "Pillow missing (pip install pillow)"
    $problems += 'pillow'
} else {
    Say "  ....  installing Pillow"
    & $python -m pip install --disable-pip-version-check -q pillow
    if ($LASTEXITCODE -eq 0) { Ok "Pillow installed" } else { Fail "Pillow install failed"; $problems += 'pillow' }
}

# ---- 3. optional OCR ----
$hasRapid = Test-Module $python 'rapidocr_onnxruntime'
if ($hasRapid) {
    Ok "RapidOCR present (best accuracy)"
} else {
    if ($WithOCR) {
        Say "  ....  installing RapidOCR (about 100 MB, one time)"
        & $python -m pip install --disable-pip-version-check -q rapidocr_onnxruntime onnxruntime
        if ($LASTEXITCODE -eq 0) { Ok "RapidOCR installed" } else { Warn "RapidOCR install failed; Windows OCR will be used" }
    } else {
        Warn "RapidOCR not installed - Windows built-in OCR will be used (fine, just weaker on small text)"
        Say "        add -WithOCR to install it"
    }
}

# ---- 4. copy the scripts ----
if ($PcDir -eq "") { $PcDir = Join-Path $env:USERPROFILE 'OllamaStart\pc' }
if ($Check) {
    if (Test-Path -LiteralPath (Join-Path $PcDir 'pc.py')) { Ok ("scripts at " + $PcDir) }
    else { Fail ("scripts missing at " + $PcDir) ; $problems += 'scripts' }
} else {
    New-Item -ItemType Directory -Force -Path $PcDir | Out-Null
    $src = Join-Path $here 'pc'
    if (-not (Test-Path -LiteralPath $src)) { Fail ("source folder not found: " + $src); exit 3 }
    # copy only the real scripts (never screenshots / logs / __pycache__)
    $files = @('pc.py', 'pctask.py', 'ocr.ps1', 'ocr_rapid.py', 'uia.ps1', 'popup_guard.py', 'dialog_guard.py')
    $copied = 0
    foreach ($f in $files) {
        $from = Join-Path $src $f
        if (Test-Path -LiteralPath $from) { Copy-Item $from (Join-Path $PcDir $f) -Force; $copied++ }
    }
    Ok ("scripts copied to " + $PcDir + "  (" + $copied + " files)")
}

# ---- 5. optional: register the plugin in the profile ----
$profileDir = Join-Path $env:USERPROFILE '.dsh\profiles\desktop'
if ($InstallPlugin) {
    if (-not (Test-Path -LiteralPath $profileDir)) {
        Warn ("DSH profile not found: " + $profileDir + " - skipping plugin registration")
    } else {
        $dst = Join-Path $profileDir 'node_modules\dsh-pc-hands'
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
        Copy-Item (Join-Path $here 'plugin\*') $dst -Force -Recurse
        $pkg = Join-Path $profileDir 'package.json'
        Copy-Item $pkg ($pkg + '.bak-pc-hands') -Force
        $py = $python
        $patch = @'
import io, json, sys
path = sys.argv[1]
d = json.loads(io.open(path, encoding='utf-8').read())
deps = d.setdefault('dependencies', {})
# keep an existing pin/version; only add one when the entry is missing
if 'dsh-pc-hands' not in deps:
    deps['dsh-pc-hands'] = 'file:./node_modules/dsh-pc-hands'
prof = d.setdefault('dsh', {}).setdefault('profile', {})
bundles = prof.setdefault('bundles', [])
if 'dsh-pc-hands' not in bundles:
    bundles.append('dsh-pc-hands')
io.open(path, 'w', encoding='utf-8', newline='\n').write(json.dumps(d, ensure_ascii=False, indent=2))
print('patched')
'@
        $patchFile = Join-Path $env:TEMP 'dsh-pc-hands-patch.py'
        [System.IO.File]::WriteAllText($patchFile, $patch, (New-Object System.Text.UTF8Encoding($false)))
        & $py $patchFile $pkg
        if ($LASTEXITCODE -eq 0) { Ok "plugin registered (profile package.json patched, backup: package.json.bak-pc-hands)" }
        else { Fail "could not patch profile package.json"; $problems += 'profile' }
    }
} elseif (-not $Check) {
    Warn "plugin NOT registered (add -InstallPlugin, or copy plugin\ into the profile yourself)"
}

# ---- summary ----
Say ""
if ($Check) {
    if ($problems.Count -eq 0) { Say "READY - everything needed is in place." }
    else { Say ("NOT READY - missing: " + ($problems -join ', ')) }
} else {
    Say "Done."
    Say ("  scripts : " + $PcDir)
    Say ("  python  : " + $python)
    if ($InstallPlugin) {
        Say "  next    : restart DSH, then ask the agent to run pc_doctor"
    } else {
        Say "  next    : install the plugin (setup.cmd -InstallPlugin) and restart DSH"
    }
}
exit 0
