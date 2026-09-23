#Requires -Version 5.1
# image | image-edit -Image | video -Reference (JPEG-compressed multipart input_reference) | video-edit -VideoId
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("image", "image-edit", "video", "video-edit")]
    [string]$Command,
    [string]$Prompt = "",
    [string]$Image = "",
    [string]$Reference = "",
    [string]$VideoId = "",
    [string]$OutDir = "",
    [string]$Size = "",
    [string]$Seconds = "4"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$script:TempUploads = New-Object System.Collections.Generic.List[string]

function Write-Err([string]$Message) { [Console]::Error.WriteLine($Message) }

function Get-Curl {
    $cmd = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "curl.exe not found" }
    return $cmd.Source
}

function Find-Ffmpeg {
    $cmd = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($cand in @(
        (Join-Path $env:USERPROFILE ".codex\bin\ffmpeg.exe"),
        "C:\ffmpeg\bin\ffmpeg.exe"
    )) { if ($cand -and (Test-Path -LiteralPath $cand)) { return $cand } }
    return $null
}

function Read-DotEnv([string]$Path) {
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        $name, $value = $line.Split("=", 2)
        $map[$name.Trim()] = $value.Trim().Trim('"').Trim("'")
    }
    return $map
}

function Join-V1([string]$Base) {
    $trim = $Base.Trim().TrimEnd("/")
    if ([string]::IsNullOrWhiteSpace($trim)) { return "" }
    if ($trim.ToLower().EndsWith("/v1")) { return $trim }
    return "$trim/v1"
}

function Resolve-Creds {
    $homeDir = $env:USERPROFILE
    if ([string]::IsNullOrWhiteSpace($homeDir)) { $homeDir = $HOME }
    if ($env:OPENAI_API_KEY -and $env:OPENAI_BASE_URL) {
        return @{ Key = $env:OPENAI_API_KEY; V1 = (Join-V1 $env:OPENAI_BASE_URL) }
    }
    $dot = Read-DotEnv (Join-Path $homeDir ".codex\.env")
    if ($dot["OPENAI_API_KEY"] -and $dot["OPENAI_BASE_URL"]) {
        return @{ Key = $dot["OPENAI_API_KEY"]; V1 = (Join-V1 $dot["OPENAI_BASE_URL"]) }
    }
    $grokToml = Join-Path $homeDir ".grok\config.toml"
    if (Test-Path -LiteralPath $grokToml) {
        $text = Get-Content -LiteralPath $grokToml -Raw -Encoding UTF8
        if ($text -match '(?s)\[model_providers\.minkingapi\](.*?)(\n\[|\z)') {
            $block = $Matches[1]; $key = $null; $base = $null
            if ($block -match 'api_key\s*=\s*"(.*?)"') { $key = $Matches[1] }
            if ($block -match 'base_url\s*=\s*"(.*?)"') { $base = $Matches[1] }
            if ($key -and $base) { return @{ Key = $key; V1 = (Join-V1 $base) } }
        }
    }
    $claudeSettings = Join-Path $homeDir ".claude\settings.json"
    if (Test-Path -LiteralPath $claudeSettings) {
        $json = Get-Content -LiteralPath $claudeSettings -Raw -Encoding UTF8 | ConvertFrom-Json
        $envMap = $json.env
        if ($envMap -and $envMap.ANTHROPIC_API_KEY) {
            $base = [string]$envMap.ANTHROPIC_BASE_URL
            if ([string]::IsNullOrWhiteSpace($base)) { $base = [string]$envMap.OPENAI_BASE_URL }
            return @{ Key = [string]$envMap.ANTHROPIC_API_KEY; V1 = (Join-V1 $base) }
        }
    }
    $wb = Join-Path $homeDir ".workbuddy\models.json"
    if (Test-Path -LiteralPath $wb) {
        $payload = Get-Content -LiteralPath $wb -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($item in @($payload.models)) {
            if ($item.apiKey -and $item.url) {
                return @{ Key = [string]$item.apiKey; V1 = (Join-V1 ([string]$item.url)) }
            }
        }
    }
    throw "MinKing API key not found. Run MinKing one-click sync first."
}

function New-OutDir {
    if ($OutDir) { return $OutDir }
    if ($env:MINKING_MEDIA_OUT) { return $env:MINKING_MEDIA_OUT }
    return (Get-Location).Path
}
function New-Stamp { return (Get-Date).ToString("yyyyMMdd-HHmmss") }

function Compact-Body([string]$Body) {
    if ([string]::IsNullOrEmpty($Body)) { return "" }
    $t = [regex]::Replace($Body, 'data:image\/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+\/=\s]+', 'data:image;base64,[redacted]')
    $t = [regex]::Replace($t, '\s+', ' ').Trim()
    if ($t.Length -gt 360) { $t = $t.Substring(0, 360) }
    return $t
}

function Get-ErrorAdvice([int]$Status, [string]$Code, [string]$Hint) {
    $blob = "$Code $Hint"
    if ($Status -eq 503 -or $blob -match 'no_healthy_accounts|cooldown') {
        return "Gateway cooldown. Wait 10 minutes. Do NOT retry."
    }
    if ($Status -in 400, 413 -or $blob -match 'length limit|too large|file_too_large|Failed to read request body') {
        return "Payload too large for Grok JSON inline. Use JPEG <=380KB. Do NOT retry the same PNG."
    }
    if ($Status -ge 500) { return "Upstream 5xx. Script retries once then stops." }
    return ""
}

function Throw-Http([string]$Op, [int]$Status, [string]$Body) {
    $code = ""; $hint = Compact-Body $Body
    try {
        $obj = $Body | ConvertFrom-Json
        if ($obj.error) {
            if ($obj.error.code) { $code = [string]$obj.error.code }
            if ($obj.error.message) { $hint = [string]$obj.error.message }
        }
        if (-not $code -and $obj.code) { $code = [string]$obj.code }
        if ($obj.message) { $hint = Compact-Body ([string]$obj.message) }
        else { $hint = Compact-Body $hint }
    } catch { }
    $advice = Get-ErrorAdvice $Status $code $hint
    throw ("$Op HTTP $Status $code $hint $advice").Trim()
}

function Convert-ImageToJpegFfmpeg([string]$Src, [string]$Dest, [int]$MaxSide, [int]$Q) {
    $ff = Find-Ffmpeg
    if (-not $ff) { return $false }
    $vf = "scale=${MaxSide}:${MaxSide}:force_original_aspect_ratio=decrease:force_divisible_by=2"
    $proc = Start-Process -FilePath $ff -ArgumentList @("-y","-hide_banner","-loglevel","error","-i",$Src,"-vf",$vf,"-q:v","$Q",$Dest) -Wait -PassThru -NoNewWindow
    return ($proc.ExitCode -eq 0 -and (Test-Path -LiteralPath $Dest) -and (Get-Item -LiteralPath $Dest).Length -gt 1000)
}

function Convert-ImageToJpegDrawing([string]$Src, [string]$Dest, [int]$MaxSide, [int]$Quality) {
    try { Add-Type -AssemblyName System.Drawing | Out-Null } catch { return $false }
    $srcImg = $null; $bmp = $null; $g = $null
    try {
        $srcImg = [System.Drawing.Image]::FromFile($Src)
        $w = $srcImg.Width; $h = $srcImg.Height
        $scale = [Math]::Min(1.0, [double]$MaxSide / [Math]::Max($w, $h))
        $nw = [int][Math]::Max(2, [Math]::Round($w * $scale))
        $nh = [int][Math]::Max(2, [Math]::Round($h * $scale))
        if ($nw % 2) { $nw-- }; if ($nh % 2) { $nh-- }
        $bmp = New-Object System.Drawing.Bitmap $nw, $nh
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.DrawImage($srcImg, 0, 0, $nw, $nh)
        $codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq "image/jpeg" } | Select-Object -First 1
        if (-not $codec) { return $false }
        $ep = New-Object System.Drawing.Imaging.EncoderParameters 1
        $ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality, [int64]$Quality)
        $bmp.Save($Dest, $codec, $ep)
        return ((Test-Path -LiteralPath $Dest) -and (Get-Item -LiteralPath $Dest).Length -gt 1000)
    } catch { return $false } finally {
        if ($g) { $g.Dispose() }; if ($bmp) { $bmp.Dispose() }; if ($srcImg) { $srcImg.Dispose() }
    }
}

function New-SafeJpeg {
    param([string]$Src, [int]$BudgetBytes, [int]$MaxSide)
    if (-not (Test-Path -LiteralPath $Src)) { throw "reference image not found: $Src" }
    $item = Get-Item -LiteralPath $Src
    if ($item.Length -lt 32) { throw "reference image is empty: $Src" }
    $tmpDir = Join-Path $env:TEMP "minking-media"
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
    $dest = Join-Path $tmpDir ("ref-" + [guid]::NewGuid().ToString("n") + ".jpg")
    $script:TempUploads.Add($dest)
    $ext = $item.Extension.ToLowerInvariant()
    if ($ext -in @(".jpg", ".jpeg") -and $item.Length -le $BudgetBytes) {
        Copy-Item -LiteralPath $Src -Destination $dest -Force
        Write-Err "upload jpeg copy bytes=$($item.Length)"
        return $dest
    }
    $ok = $false
    foreach ($side in @($MaxSide, 1024, 768, 640)) {
        foreach ($pair in @(@(5, 72), @(8, 58), @(12, 45), @(18, 32))) {
            if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Force -ErrorAction SilentlyContinue }
            if (Convert-ImageToJpegFfmpeg $Src $dest $side ([int]$pair[0])) { $ok = $true }
            elseif (Convert-ImageToJpegDrawing $Src $dest $side ([int]$pair[1])) { $ok = $true }
            if ($ok -and (Get-Item -LiteralPath $dest).Length -le $BudgetBytes) {
                Write-Err "upload jpeg compressed bytes=$((Get-Item -LiteralPath $dest).Length) side=$side"
                return $dest
            }
        }
    }
    if ($ok) { throw "could not compress reference under $BudgetBytes bytes (got $((Get-Item -LiteralPath $dest).Length)). Do not upload the original PNG." }
    throw "no jpeg encoder found (ffmpeg or System.Drawing). Install ffmpeg or pass a JPEG <=$BudgetBytes bytes."
}

function Normalize-Seconds([string]$Raw) {
    $n = 4
    if (-not [int]::TryParse($Raw, [ref]$n)) { return "4" }
    $best = 4
    foreach ($c in @(4, 6, 8)) { if ([Math]::Abs($c - $n) -lt [Math]::Abs($best - $n)) { $best = $c } }
    return "$best"
}

function Invoke-Json {
    param([string]$Method, [string]$Url, [string]$Body, [string]$OutFile, [int]$Timeout = 240)
    $curl = Get-Curl
    $tmp = [System.IO.Path]::GetTempFileName()
    $hdr = [System.IO.Path]::GetTempFileName()
    $bodyFile = $null
    try {
        $args = @("-sS", "-X", $Method, $Url, "-H", "Authorization: Bearer $($script:Creds.Key)", "-D", $hdr, "--max-time", "$Timeout")
        if ($Body) {
            $bodyFile = [System.IO.Path]::GetTempFileName()
            [System.IO.File]::WriteAllText($bodyFile, $Body, [System.Text.UTF8Encoding]::new($false))
            $args += @("-H", "Content-Type: application/json; charset=utf-8", "--data-binary", "@$bodyFile")
        }
        if ($OutFile) { $args += @("-o", $OutFile) } else { $args += @("-o", $tmp) }
        & $curl @args
        if ($LASTEXITCODE -ne 0) { throw "curl failed ($LASTEXITCODE)" }
        $status = 0
        foreach ($line in Get-Content -LiteralPath $hdr) { if ($line -match '^HTTP/\S+\s+(\d+)') { $status = [int]$Matches[1] } }
        $text = ""
        if (-not $OutFile) { $text = [System.IO.File]::ReadAllText($tmp, [System.Text.UTF8Encoding]::new($false)) }
        return @{ Status = $status; Body = $text }
    } finally {
        Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $hdr -ErrorAction SilentlyContinue
        if ($bodyFile) { Remove-Item -LiteralPath $bodyFile -ErrorAction SilentlyContinue }
    }
}

function Invoke-Multipart {
    param([string]$Url, [string[]]$Form, [int]$Timeout = 240)
    $curl = Get-Curl
    $tmp = [System.IO.Path]::GetTempFileName()
    $hdr = [System.IO.Path]::GetTempFileName()
    try {
        $args = @("-sS", "-X", "POST", $Url, "-H", "Authorization: Bearer $($script:Creds.Key)", "-D", $hdr, "--max-time", "$Timeout") + $Form + @("-o", $tmp)
        & $curl @args
        if ($LASTEXITCODE -ne 0) { throw "curl failed ($LASTEXITCODE)" }
        $status = 0
        foreach ($line in Get-Content -LiteralPath $hdr) { if ($line -match '^HTTP/\S+\s+(\d+)') { $status = [int]$Matches[1] } }
        return @{ Status = $status; Body = [System.IO.File]::ReadAllText($tmp, [System.Text.UTF8Encoding]::new($false)) }
    } finally {
        Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $hdr -ErrorAction SilentlyContinue
    }
}

function Invoke-HttpRetry {
    param([scriptblock]$Send, [string]$Op)
    $got = & $Send
    if ($got.Status -in 502, 504) {
        Write-Err "$Op HTTP $($got.Status), retry once after 3s"
        Start-Sleep -Seconds 3
        $got = & $Send
    }
    if ($got.Status -ge 400) { Throw-Http $Op $got.Status $got.Body }
    return $got
}

function Save-ImagePayload([string]$Json, [string]$Dest) {
    $obj = $Json | ConvertFrom-Json
    $item = $null
    if ($obj.data) { $item = @($obj.data)[0] }
    if (-not $item) { throw "image response had no data" }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dest) | Out-Null
    if ($item.url) {
        $got = Invoke-Json -Method GET -Url ([string]$item.url) -OutFile $Dest -Timeout 120
        if ($got.Status -ge 400) { throw "image download HTTP $($got.Status)" }
        return
    }
    if ($item.b64_json) {
        [System.IO.File]::WriteAllBytes($Dest, [Convert]::FromBase64String([string]$item.b64_json))
        return
    }
    throw "image response had neither url nor b64_json"
}

function Save-Video([string]$Video, [string]$Dest) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dest) | Out-Null
    $got = Invoke-Json -Method GET -Url "$($script:Creds.V1)/videos/$Video/content?variant=video" -OutFile $Dest -Timeout 180
    if ($got.Status -eq 409) { return $false }
    if ($got.Status -ge 400) { Throw-Http "video content" $got.Status "" }
    if (-not (Test-Path -LiteralPath $Dest) -or (Get-Item -LiteralPath $Dest).Length -lt 10000) { throw "video content too small" }
    return $true
}

function Wait-Video([string]$Video) {
    $deadline = (Get-Date).AddMinutes(10)
    while ((Get-Date) -lt $deadline) {
        $got = Invoke-Json -Method GET -Url "$($script:Creds.V1)/videos/$Video" -Timeout 60
        if ($got.Status -ge 400) { Throw-Http "video poll" $got.Status $got.Body }
        $obj = $got.Body | ConvertFrom-Json
        $status = [string]$obj.status
        Write-Err "video $Video $status $($obj.progress)"
        if ($status -eq "completed") { return }
        if ($status -eq "failed") { throw "video failed $(Compact-Body ($got.Body))" }
        Start-Sleep -Seconds 3
    }
    throw "video poll timed out"
}

if ([string]::IsNullOrWhiteSpace($Prompt)) { Write-Err "prompt is required"; exit 2 }
$script:Creds = Resolve-Creds
if (-not $script:Creds.V1) { Write-Err "MinKing base URL not found"; exit 2 }
$outRoot = New-OutDir
$stamp = New-Stamp
$Seconds = Normalize-Seconds $Seconds

try {
    if ($Command -eq "image") {
        $payload = @{ prompt = $Prompt; response_format = "url" }
        if ($Size) { $payload.size = $Size }
        $got = Invoke-HttpRetry -Op "images/generations" -Send { Invoke-Json -Method POST -Url "$($script:Creds.V1)/images/generations" -Body ($payload | ConvertTo-Json -Compress) }
        $dest = Join-Path $outRoot "minking-image-$stamp.png"
        Save-ImagePayload $got.Body $dest
        Write-Output ((Resolve-Path -LiteralPath $dest).Path)
        exit 0
    }

    if ($Command -eq "image-edit") {
        if (-not $Image) { throw "image-edit requires -Image" }
        $form = @("--form-string", "prompt=$Prompt", "--form-string", "response_format=url")
        if (Test-Path -LiteralPath $Image) {
            $safe = New-SafeJpeg -Src $Image -BudgetBytes 700000 -MaxSide 1536
            $form += @("-F", "image=@${safe};type=image/jpeg;filename=edit.jpg")
        } else {
            $form += @("--form-string", "image=$Image")
        }
        if ($Size) { $form += @("--form-string", "size=$Size") }
        $got = Invoke-HttpRetry -Op "images/edits" -Send { Invoke-Multipart -Url "$($script:Creds.V1)/images/edits" -Form $form }
        $dest = Join-Path $outRoot "minking-image-$stamp.png"
        Save-ImagePayload $got.Body $dest
        Write-Output ((Resolve-Path -LiteralPath $dest).Path)
        exit 0
    }

    if ($Command -eq "video") {
        if ($Reference -and (Test-Path -LiteralPath $Reference)) {
            $safe = New-SafeJpeg -Src $Reference -BudgetBytes 380000 -MaxSide 1280
            $form = @(
                "--form-string", "prompt=$Prompt",
                "--form-string", "seconds=$Seconds",
                "--form-string", "model=grok-imagine-video-1.5",
                "-F", "input_reference=@${safe};type=image/jpeg;filename=ref.jpg"
            )
            if ($Size) { $form += @("--form-string", "size=$Size") }
            $got = Invoke-HttpRetry -Op "videos" -Send { Invoke-Multipart -Url "$($script:Creds.V1)/videos" -Form $form -Timeout 60 }
        } else {
            $payload = @{ prompt = $Prompt; model = "grok-imagine-video-1.5"; seconds = "$Seconds" }
            if ($Size) { $payload.size = $Size }
            if ($Reference) { $payload.input_reference = $Reference }
            $body = $payload | ConvertTo-Json -Compress
            $got = Invoke-HttpRetry -Op "videos" -Send { Invoke-Json -Method POST -Url "$($script:Creds.V1)/videos" -Body $body -Timeout 60 }
        }
        $created = $got.Body | ConvertFrom-Json
        $vid = [string]$created.id
        if (-not $vid) { throw "videos response had no id" }
        Wait-Video $vid
        $dest = Join-Path $outRoot "minking-video-$stamp.mp4"
        $ready = $false
        for ($i = 0; $i -lt 20; $i++) { if (Save-Video $vid $dest) { $ready = $true; break }; Start-Sleep -Seconds 3 }
        if (-not $ready) { throw "video content not ready" }
        Write-Output ((Resolve-Path -LiteralPath $dest).Path)
        exit 0
    }

    if ($Command -eq "video-edit") {
        if (-not $VideoId) { throw "video-edit requires -VideoId" }
        $payload = @{ prompt = $Prompt; model = "grok-imagine-video"; video = @{ id = $VideoId } }
        $body = $payload | ConvertTo-Json -Compress -Depth 5
        $got = Invoke-HttpRetry -Op "videos/edits" -Send { Invoke-Json -Method POST -Url "$($script:Creds.V1)/videos/edits" -Body $body -Timeout 60 }
        $created = $got.Body | ConvertFrom-Json
        $vid = [string]$created.id
        if (-not $vid) { throw "videos/edits response had no id" }
        Wait-Video $vid
        $dest = Join-Path $outRoot "minking-video-$stamp.mp4"
        $ready = $false
        for ($i = 0; $i -lt 20; $i++) { if (Save-Video $vid $dest) { $ready = $true; break }; Start-Sleep -Seconds 3 }
        if (-not $ready) { throw "video content not ready" }
        Write-Output ((Resolve-Path -LiteralPath $dest).Path)
        exit 0
    }
} catch {
    Write-Err $_.Exception.Message
    exit 1
} finally {
    foreach ($f in $script:TempUploads) { Remove-Item -LiteralPath $f -ErrorAction SilentlyContinue }
}
