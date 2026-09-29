<#
    check_public_release.ps1
    Public-release gate for this repository.

    Fails (exit != 0) when the tree contains anything that must not be published,
    or when the tools/docs have basic integrity problems.

    Checks:
      1. Forbidden binary / game-asset file types
      2. Oversized files (default > 2 MB)
      3. Privacy leaks (absolute Windows paths, user dirs, e-mail, secrets)
      4. Python syntax (py_compile) for every tools/*.py
      5. Import closure (local imports must resolve; 3rd-party must be importable)
      6. Document references (docs/NN_*.md must exist unless known-unpublished)
      7. Encoding (UTF-8 without BOM; no replacement characters)

    Usage:
      powershell -NoProfile -ExecutionPolicy Bypass -File tools\check_public_release.ps1
#>
[CmdletBinding()]
param(
    [string]$Root,
    [int]$MaxFileMB = 2
)

if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$Root = (Resolve-Path $Root).Path

$script:Fail = 0
$script:Warn = 0

function Fail([string]$msg) { $script:Fail++; Write-Host ("  [FAIL] " + $msg) -ForegroundColor Red }
function Warn([string]$msg) { $script:Warn++; Write-Host ("  [warn] " + $msg) -ForegroundColor Yellow }
function Ok([string]$msg)   { Write-Host ("  [ ok ] " + $msg) -ForegroundColor Green }
function Head([string]$msg) { Write-Host ""; Write-Host ("== " + $msg) -ForegroundColor Cyan }

# Everything except .git and generated caches
$files = Get-ChildItem -Path $Root -Recurse -File -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\\.git\\' -and $_.FullName -notmatch '\\__pycache__\\' }

Write-Host ("Repository : " + $Root)
Write-Host ("Files      : " + $files.Count)

# ---------------------------------------------------------------- 1. binaries
Head "1. Forbidden file types"
$badExt = @('.exe', '.dll', '.pack', '.pdb', '.gbf', '.rep', '.sqlite', '.db',
            '.bin', '.zip', '.7z', '.rar', '.iso', '.pyc', '.pdb',
            '.png', '.jpg', '.jpeg', '.gif', '.tga', '.bmp', '.wav', '.mp3', '.mp4')
$hits = $files | Where-Object { $badExt -contains $_.Extension.ToLower() }
if ($hits) { $hits | ForEach-Object { Fail ("forbidden type: " + $_.FullName.Substring($Root.Length + 1)) } }
else { Ok "no forbidden file types" }

# ---------------------------------------------------------------- 2. size
Head ("2. File size (limit " + $MaxFileMB + " MB)")
$big = $files | Where-Object { $_.Length -gt ($MaxFileMB * 1MB) }
if ($big) { $big | ForEach-Object { Fail ("too large: " + [math]::Round($_.Length / 1MB, 1) + " MB " + $_.FullName.Substring($Root.Length + 1)) } }
else { Ok "no oversized files" }

# ---------------------------------------------------------------- 3. privacy
Head "3. Privacy leaks"
$textExt = @('.md', '.py', '.ps1', '.txt', '.json', '.yml', '.yaml', '.cfg', '.toml', '.spec')
$texts = $files | Where-Object { $textExt -contains $_.Extension.ToLower() }

# Absolute drive-letter paths are flagged; bare relative components (e.g. steamapps) are fine.
$pAbs    = '[A-Za-z]:[\\/]'
$pUser   = 'C:[\\/]Users[\\/]'
$pMail   = '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
$pSecret = '(?i)(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*[\x22\x27]?[A-Za-z0-9._-]{12,}'

# Files allowed to contain absolute paths (none by default; keep the list explicit).
$absAllow = @()

$privHits = 0
foreach ($f in $texts) {
    $rel = $f.FullName.Substring($Root.Length + 1)
    $raw = [System.IO.File]::ReadAllText($f.FullName, [System.Text.Encoding]::UTF8)
    $lineNo = 0
    foreach ($line in ($raw -split "\r?\n")) {
        $lineNo++
        $isAbsAllowed = $false
        foreach ($a in $absAllow) { if ($rel -like $a) { $isAbsAllowed = $true } }
        if ((-not $isAbsAllowed) -and $line -match $pAbs) { Fail ($rel + ":" + $lineNo + " absolute path -> " + $line.Trim()); $privHits++ }
        if ($line -match $pUser)   { Fail ($rel + ":" + $lineNo + " user directory -> " + $line.Trim()); $privHits++ }
        if ($line -match $pMail)   { Fail ($rel + ":" + $lineNo + " e-mail -> " + $line.Trim()); $privHits++ }
        if ($line -match $pSecret) { Fail ($rel + ":" + $lineNo + " possible secret -> " + $line.Trim()); $privHits++ }
    }
}
if ($privHits -eq 0) { Ok "no privacy leaks in text files" }

# ---------------------------------------------------------------- 4/5. python
Head "4. Python syntax + 5. import closure"
$pyFiles = $files | Where-Object { $_.Extension -eq '.py' }
if (-not $pyFiles) { Warn "no .py files found" }
else {
    $parseErr = 0
    foreach ($f in $pyFiles) {
        $out = & python -c "import py_compile,sys; py_compile.compile(sys.argv[1], doraise=True)" $f.FullName 2>&1
        if ($LASTEXITCODE -ne 0) { Fail ("py_compile: " + $f.FullName.Substring($Root.Length + 1) + " -> " + ($out -join ' ')); $parseErr++ }
    }
    if ($parseErr -eq 0) { Ok ($pyFiles.Count.ToString() + " python files compile") }

    # local module names available in the tree
    $local = @{}
    foreach ($f in $pyFiles) { $local[[System.IO.Path]::GetFileNameWithoutExtension($f.Name)] = $true }

    # collect imported top-level module names
    $imports = @{}
    foreach ($f in $pyFiles) {
        $raw = [System.IO.File]::ReadAllText($f.FullName, [System.Text.Encoding]::UTF8)
        foreach ($m in [regex]::Matches($raw, '(?m)^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)')) {
            $imports[$m.Groups[1].Value] = $true
        }
    }
    $external = @()
    foreach ($n in $imports.Keys) { if (-not $local.ContainsKey($n)) { $external += $n } }

    if ($external.Count -gt 0) {
        # NOTE: keep the snippet free of quote characters - PS 5.1 mangles them
        # when passing arguments to a native command.
        $nl = [char]10
        $probe = 'import importlib,sys' + $nl +
                 'for n in sys.argv[1:]:' + $nl +
                 ' try: importlib.import_module(n)' + $nl +
                 ' except Exception: print(n)'
        $bad = & python -c $probe @external 2>&1
        $badStr = (($bad | Out-String) -replace "\r?\n", " ").Trim()
        if ($LASTEXITCODE -ne 0) { Fail ("import probe failed: " + $badStr) }
        elseif ($badStr -ne "") { Fail ("unresolvable imports: " + $badStr) }
        else { Ok ("all imports resolve (" + $external.Count + " external, " + $local.Count + " local)") }
    }

    # local imports must actually exist as a .py in the tree
    $missing = @()
    foreach ($f in $pyFiles) {
        $raw = [System.IO.File]::ReadAllText($f.FullName, [System.Text.Encoding]::UTF8)
        foreach ($m in [regex]::Matches($raw, '(?m)^\s*import\s+([A-Za-z_][A-Za-z0-9_]*)\s+as\s+')) {
            $n = $m.Groups[1].Value
            if ((-not $local.ContainsKey($n)) -and ($external -notcontains $n)) { $missing += ($f.Name + " -> " + $n) }
        }
    }
    if ($missing.Count -gt 0) { $missing | ForEach-Object { Fail ("unresolved local import: " + $_) } }
}

# ---------------------------------------------------------------- 6. docs
Head "6. Document references"
$docsDir = Join-Path $Root 'docs'
if (Test-Path $docsDir) {
    $present = @{}
    Get-ChildItem $docsDir -File | ForEach-Object { $present[$_.Name] = $true }
    # Docs intentionally NOT published (logbooks / goal-4 research). Referencing these is expected.
    $unpublished = @(
        '12_GOAL1_LOGBOOK.md', '22_GOAL2_LOGBOOK.md', '62_GOAL3_1_LOGBOOK.md',
        'Goal_3_LogBook.md', 'Goal_4_LogBook.md',
        '15_GOAL4_EXPLORATION_MAP.md', '41_GOAL4_AI_MECHANISM_MAP.md'
    )
    # Only references explicitly qualified as docs/<name>.md are enforced.
    # Bare names may point into the author's private work directory (see docs/README.md).
    $dangling = @{}
    foreach ($f in (Get-ChildItem $docsDir -File -Filter '*.md')) {
        $raw = [System.IO.File]::ReadAllText($f.FullName, [System.Text.Encoding]::UTF8)
        foreach ($m in [regex]::Matches($raw, 'docs[\\/]([0-9A-Za-z_\-]+\.md)')) {
            $n = $m.Groups[1].Value
            if ((-not $present.ContainsKey($n)) -and ($unpublished -notcontains $n)) { $dangling[$n] = $true }
        }
    }
    if ($dangling.Count -gt 0) { $dangling.Keys | Sort-Object | ForEach-Object { Fail ("dangling doc reference: " + $_) } }
    else { Ok ("all doc references resolve (" + $present.Count + " docs present)") }
} else { Warn "no docs/ directory" }

# ---------------------------------------------------------------- 7. encoding
Head "7. Encoding (UTF-8, no BOM, no replacement chars)"
$enc = 0
foreach ($f in $texts) {
    $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        Fail ("UTF-8 BOM: " + $f.FullName.Substring($Root.Length + 1)); $enc++
    }
    $txt = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ($txt.Contains([char]0xFFFD)) {
        Fail ("replacement char (encoding damage): " + $f.FullName.Substring($Root.Length + 1)); $enc++
    }
}
if ($enc -eq 0) { Ok "encoding clean" }

# ---------------------------------------------------------------- summary
Write-Host ""
Write-Host ("RESULT: " + $(if ($script:Fail -eq 0) { "PASS" } else { "FAIL" }) + "  (fail=$($script:Fail) warn=$($script:Warn))") -ForegroundColor $(if ($script:Fail -eq 0) { 'Green' } else { 'Red' })
exit $script:Fail
