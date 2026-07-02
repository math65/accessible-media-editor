[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [string]$ReleasesApiUrl = "https://api.github.com/repos/GyanD/codexffmpeg/releases?per_page=20",
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BinDir = Join-Path $ProjectRoot "bin"
$FfmpegPath = Join-Path $BinDir "ffmpeg.exe"
$FfprobePath = Join-Path $BinDir "ffprobe.exe"
$UserAgent = "AccessibleMediaConverter-FFmpeg-Updater"

function Get-ToolVersionLine {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ToolPath
    )

    if (-not (Test-Path -LiteralPath $ToolPath)) {
        throw "Binary not found: $ToolPath"
    }

    $output = & $ToolPath -version 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $output) {
        throw "Unable to read version from $ToolPath"
    }

    return [string]($output | Select-Object -First 1)
}

function Get-VersionToken {
    param(
        [Parameter(Mandatory = $true)]
        [string]$VersionLine
    )

    $match = [regex]::Match($VersionLine, 'version\s+(?<token>\S+)')
    if ($match.Success) {
        return $match.Groups['token'].Value
    }

    return ""
}

function Get-SemanticVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $match = [regex]::Match($Value, '^(?<version>\d+\.\d+(?:\.\d+)?)')
    if ($match.Success) {
        return $match.Groups['version'].Value
    }

    return ""
}

function ConvertTo-VersionTuple {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SemanticVersion
    )

    if (-not $SemanticVersion) {
        return $null
    }

    $parts = @($SemanticVersion.Split('.') | ForEach-Object { [int]$_ })
    while ($parts.Count -lt 3) {
        $parts += 0
    }

    return [int[]]$parts[0..2]
}

function Compare-VersionTuples {
    param(
        [Parameter(Mandatory = $true)]
        [int[]]$Left,
        [Parameter(Mandatory = $true)]
        [int[]]$Right
    )

    for ($index = 0; $index -lt 3; $index++) {
        if ($Left[$index] -gt $Right[$index]) {
            return 1
        }
        if ($Left[$index] -lt $Right[$index]) {
            return -1
        }
    }

    return 0
}

function Get-BuildDate {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $match = [regex]::Match($Value, '^(?<date>\d{4}-\d{2}-\d{2})')
    if (-not $match.Success) {
        return $null
    }

    return [datetime]::ParseExact(
        $match.Groups['date'].Value,
        'yyyy-MM-dd',
        [System.Globalization.CultureInfo]::InvariantCulture
    ).Date
}

function Get-EmbeddedBinaryMetadata {
    param(
        [Parameter(Mandatory = $true)]
        [string]$VersionLine
    )

    $token = Get-VersionToken -VersionLine $VersionLine
    $semanticVersion = Get-SemanticVersion -Value $token
    $buildDate = Get-BuildDate -Value $token
    $semanticTuple = $null
    if ($semanticVersion) {
        $semanticTuple = ConvertTo-VersionTuple -SemanticVersion $semanticVersion
    }

    return [pscustomobject]@{
        VersionLine      = $VersionLine
        Token            = $token
        SemanticVersion  = $semanticVersion
        SemanticTuple    = $semanticTuple
        BuildDate        = $buildDate
    }
}

function Get-ReleaseMetadata {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Release
    )

    $tagName = [string]($Release.tag_name | ForEach-Object { $_ })
    $semanticVersion = Get-SemanticVersion -Value $tagName
    $publishedDate = $null
    $semanticTuple = $null

    if ($semanticVersion) {
        $semanticTuple = ConvertTo-VersionTuple -SemanticVersion $semanticVersion
    }

    if ($Release.published_at) {
        $publishedDate = ([datetime]$Release.published_at).Date
    }

    return [pscustomobject]@{
        TagName          = $tagName
        SemanticVersion  = $semanticVersion
        SemanticTuple    = $semanticTuple
        PublishedDate    = $publishedDate
        HtmlUrl          = [string]$Release.html_url
    }
}

function Get-UpdateDecision {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject]$Current,
        [Parameter(Mandatory = $true)]
        [pscustomobject]$Latest
    )

    # The GyanD release tag (e.g. 2026-05-28-git-7b46c6a2a3) is the exact build
    # token, and the embedded ffmpeg -version line starts with that same token.
    # Comparing them directly is more reliable than date heuristics and needs no
    # download — so we can answer "already up to date" purely from API metadata.
    if ($Current.Token -and $Latest.TagName -and $Current.Token.StartsWith($Latest.TagName)) {
        return [pscustomobject]@{
            ShouldDownload = $false
            Reason = "Embedded FFmpeg already matches the latest GitHub release $($Latest.TagName)."
        }
    }

    if ($Current.SemanticTuple -and $Latest.SemanticTuple) {
        $semanticComparison = Compare-VersionTuples -Left $Current.SemanticTuple -Right $Latest.SemanticTuple
        if ($semanticComparison -gt 0) {
            return [pscustomobject]@{
                ShouldDownload = $false
                Reason = "Embedded FFmpeg semantic version $($Current.SemanticVersion) is newer than GitHub release $($Latest.SemanticVersion)."
            }
        }
        if ($semanticComparison -eq 0) {
            return [pscustomobject]@{
                ShouldDownload = $false
                Reason = "Embedded FFmpeg semantic version already matches GitHub release $($Latest.SemanticVersion)."
            }
        }
    }

    if ($Current.BuildDate -and $Latest.PublishedDate) {
        if ($Current.BuildDate -gt $Latest.PublishedDate) {
            return [pscustomobject]@{
                ShouldDownload = $false
                Reason = "Embedded FFmpeg build date $($Current.BuildDate.ToString('yyyy-MM-dd')) is newer than the latest GitHub release date $($Latest.PublishedDate.ToString('yyyy-MM-dd'))."
            }
        }
        if ($Current.BuildDate -eq $Latest.PublishedDate) {
            return [pscustomobject]@{
                ShouldDownload = $false
                Reason = "Embedded FFmpeg build date already matches the latest GitHub release date $($Latest.PublishedDate.ToString('yyyy-MM-dd'))."
            }
        }
    }

    return [pscustomobject]@{
        ShouldDownload = $true
        Reason = "A newer FFmpeg release may be available on GitHub."
    }
}

function Get-LatestReleaseWithEssentialsZip {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ApiUrl
    )

    try {
        $releases = Invoke-RestMethod -Uri $ApiUrl -Headers @{
            "Accept"     = "application/vnd.github+json"
            "User-Agent" = $UserAgent
        }
    }
    catch {
        throw "Unable to fetch FFmpeg releases from GitHub API: $ApiUrl"
    }

    $release = $releases |
        Where-Object {
            @($_.assets) | Where-Object { $_.name -match '^ffmpeg-.*-essentials_build\.zip$' }
        } |
        Select-Object -First 1

    if ($release) {
        return $release
    }

    throw "Unable to find a published FFmpeg release with an essentials ZIP asset."
}

function Get-EssentialsZipAsset {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Release
    )

    $asset = @($Release.assets) |
        Where-Object { $_.name -match '^ffmpeg-.*-essentials_build\.zip$' } |
        Select-Object -First 1

    if (-not $asset) {
        throw "Unable to find an essentials ZIP asset in the selected FFmpeg release."
    }

    return $asset
}

function Test-ArchiveDigest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ArchivePath,
        [Parameter(Mandatory = $false)]
        [string]$Digest
    )

    if (-not $Digest) {
        return
    }

    if ($Digest -notmatch '^sha256:(?<value>[0-9a-fA-F]{64})$') {
        Write-Warning "Unsupported digest format reported by GitHub: $Digest"
        return
    }

    $expectedHash = $Matches.value.ToLowerInvariant()
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ArchivePath).Hash.ToLowerInvariant()

    if ($actualHash -ne $expectedHash) {
        throw "Downloaded archive hash mismatch. Expected $expectedHash but got $actualHash."
    }
}

function Find-ExtractedBinary {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ExtractRoot,
        [Parameter(Mandatory = $true)]
        [string]$FileName
    )

    $matches = @(Get-ChildItem -Path $ExtractRoot -Recurse -File -Filter $FileName)
    if (-not $matches) {
        throw "Unable to find $FileName in the extracted archive."
    }

    $preferred = $matches | Where-Object { $_.DirectoryName -match '[\\/]bin$' } | Select-Object -First 1
    if ($preferred) {
        return $preferred.FullName
    }

    return $matches[0].FullName
}

function Restore-BinaryFromBackup {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TargetPath,
        [Parameter(Mandatory = $true)]
        [string]$BackupPath,
        [Parameter(Mandatory = $true)]
        [bool]$PreviouslyExisted
    )

    if ($PreviouslyExisted -and (Test-Path -LiteralPath $BackupPath)) {
        Copy-Item -LiteralPath $BackupPath -Destination $TargetPath -Force
        return
    }

    if (-not $PreviouslyExisted -and (Test-Path -LiteralPath $TargetPath)) {
        Remove-Item -LiteralPath $TargetPath -Force
    }
}

function Invoke-WithoutWhatIf {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$ScriptBlock
    )

    $previousWhatIfPreference = $WhatIfPreference
    try {
        $WhatIfPreference = $false
        & $ScriptBlock
    }
    finally {
        $WhatIfPreference = $previousWhatIfPreference
    }
}

$currentFfmpegVersion = Get-ToolVersionLine -ToolPath $FfmpegPath
$currentFfprobeVersion = Get-ToolVersionLine -ToolPath $FfprobePath
$currentMetadata = Get-EmbeddedBinaryMetadata -VersionLine $currentFfmpegVersion

Write-Host "Current embedded FFmpeg : $currentFfmpegVersion"
Write-Host "Current embedded FFprobe: $currentFfprobeVersion"
if ($currentMetadata.SemanticVersion) {
    Write-Host "Current semantic version: $($currentMetadata.SemanticVersion)"
}
if ($currentMetadata.BuildDate) {
    Write-Host "Current build date      : $($currentMetadata.BuildDate.ToString('yyyy-MM-dd'))"
}

$release = Get-LatestReleaseWithEssentialsZip -ApiUrl $ReleasesApiUrl
$asset = Get-EssentialsZipAsset -Release $release
$releaseMetadata = Get-ReleaseMetadata -Release $release

Write-Host "Latest GitHub release: $($release.tag_name)"
Write-Host "Selected asset      : $($asset.name)"
if ($releaseMetadata.SemanticVersion) {
    Write-Host "Latest semantic version: $($releaseMetadata.SemanticVersion)"
}
if ($releaseMetadata.PublishedDate) {
    Write-Host "Latest release date    : $($releaseMetadata.PublishedDate.ToString('yyyy-MM-dd'))"
}

$decision = Get-UpdateDecision -Current $currentMetadata -Latest $releaseMetadata
Write-Host $decision.Reason

if (-not $decision.ShouldDownload) {
    return
}

# A dry run only needs to know whether an update exists. The release tag already
# tells us that, so report it and stop before downloading the ~80 MB archive.
if ($CheckOnly) {
    Write-Host "An embedded FFmpeg update is available: $($release.tag_name)"
    Write-Host "Run without -CheckOnly to download, verify, and install it."
    Write-Host "Release page: $($release.html_url)"
    return
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("amc-ffmpeg-update-" + [Guid]::NewGuid().ToString("N"))
$downloadPath = Join-Path $tempRoot $asset.name
$extractRoot = Join-Path $tempRoot "extract"
$backupRoot = Join-Path $tempRoot "backup"

[void][System.IO.Directory]::CreateDirectory($tempRoot)
[void][System.IO.Directory]::CreateDirectory($extractRoot)
[void][System.IO.Directory]::CreateDirectory($backupRoot)

try {
    Write-Host "Downloading latest essentials archive..."
    Invoke-WithoutWhatIf {
        Invoke-WebRequest -Uri $asset.browser_download_url -Headers @{
            "Accept"     = "application/octet-stream"
            "User-Agent" = $UserAgent
        } -OutFile $downloadPath
    }

    Test-ArchiveDigest -ArchivePath $downloadPath -Digest $asset.digest

    Write-Host "Extracting archive..."
    Invoke-WithoutWhatIf {
        Expand-Archive -LiteralPath $downloadPath -DestinationPath $extractRoot -Force
    }

    $candidateFfmpegPath = Find-ExtractedBinary -ExtractRoot $extractRoot -FileName "ffmpeg.exe"
    $candidateFfprobePath = Find-ExtractedBinary -ExtractRoot $extractRoot -FileName "ffprobe.exe"

    $candidateFfmpegVersion = Get-ToolVersionLine -ToolPath $candidateFfmpegPath
    $candidateFfprobeVersion = Get-ToolVersionLine -ToolPath $candidateFfprobePath

    Write-Host "Candidate FFmpeg    : $candidateFfmpegVersion"
    Write-Host "Candidate FFprobe   : $candidateFfprobeVersion"

    if ($candidateFfmpegVersion -eq $currentFfmpegVersion -and $candidateFfprobeVersion -eq $currentFfprobeVersion) {
        Write-Host "Embedded FFmpeg binaries are already up to date."
        return
    }

    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null

    $backupFfmpegPath = Join-Path $backupRoot "ffmpeg.exe"
    $backupFfprobePath = Join-Path $backupRoot "ffprobe.exe"
    $hadFfmpeg = Test-Path -LiteralPath $FfmpegPath
    $hadFfprobe = Test-Path -LiteralPath $FfprobePath

    if ($hadFfmpeg) {
        Copy-Item -LiteralPath $FfmpegPath -Destination $backupFfmpegPath -Force
    }
    if ($hadFfprobe) {
        Copy-Item -LiteralPath $FfprobePath -Destination $backupFfprobePath -Force
    }

    if ($PSCmdlet.ShouldProcess($BinDir, "Replace embedded FFmpeg binaries from release $($release.tag_name)")) {
        try {
            Copy-Item -LiteralPath $candidateFfmpegPath -Destination $FfmpegPath -Force
            Copy-Item -LiteralPath $candidateFfprobePath -Destination $FfprobePath -Force

            $installedFfmpegVersion = Get-ToolVersionLine -ToolPath $FfmpegPath
            $installedFfprobeVersion = Get-ToolVersionLine -ToolPath $FfprobePath

            if ($installedFfmpegVersion -ne $candidateFfmpegVersion) {
                throw "Installed ffmpeg.exe version does not match the downloaded candidate."
            }
            if ($installedFfprobeVersion -ne $candidateFfprobeVersion) {
                throw "Installed ffprobe.exe version does not match the downloaded candidate."
            }

            Write-Host "Embedded FFmpeg binaries updated successfully."
            Write-Host "Release page: $($release.html_url)"
        }
        catch {
            Restore-BinaryFromBackup -TargetPath $FfmpegPath -BackupPath $backupFfmpegPath -PreviouslyExisted $hadFfmpeg
            Restore-BinaryFromBackup -TargetPath $FfprobePath -BackupPath $backupFfprobePath -PreviouslyExisted $hadFfprobe
            throw
        }
    }
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Invoke-WithoutWhatIf {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force
        }
    }
}
