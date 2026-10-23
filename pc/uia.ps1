# uia.ps1 - UI Automation helper for the pc toolset.
#
# Talks to .NET UIAutomation instead of pixels: find a control by its Name, then
# Invoke / Select it. No coordinates, no foreground, and it works on controls
# that are scrolled out of view (UIA can BringIntoView first).
#
# All output is ASCII on purpose: Chinese literals inside a .ps1 are a known
# encoding trap on this machine.
#
#   uia.ps1 list  <hwnd> [nameSubstring] [maxRows]
#   uia.ps1 name  <hwnd> <exactName>          -> prints index + rect + patterns
#   uia.ps1 invoke <hwnd> <exactName>
#   uia.ps1 select <hwnd> <exactName>
#   uia.ps1 focus  <hwnd> <exactName>

param(
    [Parameter(Mandatory = $true)][string]$Verb,
    [Parameter(Mandatory = $true)][string]$Hwnd,
    [string]$Name = "",
    [int]$Max = 60
)

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

# Force UTF-8 stdout: Node spawns this script, and the console then
# defaults to the system code page (GBK), garbling Chinese names.
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

$h = [IntPtr][int]$Hwnd
$root = [System.Windows.Automation.AutomationElement]::FromHandle($h)
if ($null -eq $root) { Write-Output "ERROR no element for hwnd $Hwnd"; exit 1 }

$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition)

function Get-PatternName($el) {
    $names = @()
    foreach ($p in @("InvokePattern", "SelectionItemPattern", "TogglePattern", "ValuePattern", "ExpandCollapsePattern")) {
        $pat = $null
        if ($el.TryGetCurrentPattern([System.Windows.Automation.AutomationPattern]::LookupById(
                    @{ "InvokePattern" = 10000; "SelectionItemPattern" = 10010; "TogglePattern" = 10015;
                       "ValuePattern" = 10002; "ExpandCollapsePattern" = 10005 }[$p]), [ref]$pat)) {
            $names += $p.Replace("Pattern", "")
        }
    }
    return ($names -join ",")
}

function Get-Rect($el) {
    $r = $el.Current.BoundingRectangle
    if ([double]::IsInfinity($r.X) -or [double]::IsNaN($r.X)) { return "none" }
    return ("{0},{1},{2},{3}" -f [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height)
}

if ($Verb -eq "tree") {
    # Print containers (window / pane / group / list / toolbar) with depth so a
    # caller can say "the item inside THIS container" instead of guessing pixels.
    $depth = if ($Max -gt 0) { $Max } else { 4 }
    function Walk($el, $level) {
        if ($level -gt $depth) { return }
        $c = $walker.GetFirstChild($el)
        while ($null -ne $c) {
            $cur = $c.Current
            $type = $cur.ControlType.ProgrammaticName.Replace("ControlType.", "")
            $nm = $cur.Name
            if ([string]::IsNullOrWhiteSpace($nm)) { $nm = "-" }
            Write-Output ((("  " * $level) + $type + " | " + $nm + " | " + (Get-Rect $c)))
            Walk $c ($level + 1)
            $c = $walker.GetNextSibling($c)
        }
    }
    Walk $root 0
    exit 0
}

if ($Verb -eq "list") {
    $i = 0
    $shown = 0
    foreach ($el in $all) {
        $c = $el.Current
        $nm = $c.Name
        if ([string]::IsNullOrWhiteSpace($nm)) { continue }
        if ($Name -ne "" -and $nm -notlike "*$Name*") { continue }
        $i++
        if ($shown -ge $Max) { break }
        $shown++
        Write-Output ("[{0}] type={1} name={2} rect={3} patterns={4}" -f `
                $i, $c.ControlType.ProgrammaticName.Replace("ControlType.", ""), $nm, (Get-Rect $el), (Get-PatternName $el))
    }
    Write-Output "TOTAL matched=$i shown=$shown of=$($all.Count)"
    exit 0
}

# Several elements can share a name (Explorer exposes a Group and a Text label
# with the same string as the tile). Pick the one that can actually act.
$cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty, $Name)
$matches = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
if ($matches.Count -eq 0) { Write-Output "ERROR no control named: $Name"; exit 1 }

$wantPattern = switch ($Verb) {
    "select" { [System.Windows.Automation.SelectionItemPattern]::Pattern }
    "invoke" { [System.Windows.Automation.InvokePattern]::Pattern }
    default {
        if ($Verb -eq "name") { [System.Windows.Automation.SelectionItemPattern]::Pattern } else { $null }
    }
}

$el = $null
foreach ($cand in $matches) {
    if ($null -ne $wantPattern) {
        $tmp = $null
        if ($cand.TryGetCurrentPattern($wantPattern, [ref]$tmp)) { $el = $cand; break }
    } else {
        $el = $cand; break
    }
}
if ($null -eq $el) {
    if ($Verb -eq "select" -or $Verb -eq "invoke") {
        Write-Output ("ERROR no actionable element named: $Name (found " + $matches.Count + ")")
        exit 1
    }
    $el = $matches[0]
}

switch ($Verb) {
    "name" {
        Write-Output ("name={0} type={1} rect={2} patterns={3}" -f $Name,
            $el.Current.ControlType.ProgrammaticName.Replace("ControlType.", ""), (Get-Rect $el), (Get-PatternName $el))
    }
    "invoke" {
        $pat = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $pat.Invoke()
        Write-Output "invoked: $Name"
    }
    "select" {
        try { $el.SetFocus() } catch { }
        $pat = $el.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
        $pat.Select()
        Write-Output ("selected: {0} rect={1} type={2} patterns={3}" -f $Name, (Get-Rect $el),
            $el.Current.ControlType.ProgrammaticName.Replace("ControlType.", ""), (Get-PatternName $el))
    }
    "focus" {
        try { $el.SetFocus() } catch { Write-Output ("focus failed: " + $_.Exception.Message) }
        Write-Output "focused: $Name"
    }
    default { Write-Output "ERROR unknown verb: $Verb"; exit 1 }
}
