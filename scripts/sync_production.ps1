param(
    [string]$Branch = "main",
    [string]$Server = "alias-danmu",
    [string]$ServerRepo = "/opt/Linux_bot_repo",
    [string]$RuntimeDir = "/opt/Linux_bot",
    [string]$ServiceName = "qqbot",
    [int]$Retries = 5,
    [int]$DelaySeconds = 5,
    [switch]$SkipGithub,
    [switch]$NoBundleFallback
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$File,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host "> $File $($Arguments -join ' ')"
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$File exited with code $LASTEXITCODE"
    }
}

function Invoke-WithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [int]$Attempts = $Retries
    )

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            Write-Host "[$Name] attempt $attempt/$Attempts"
            & $Action
            return $true
        } catch {
            Write-Warning "[$Name] failed: $($_.Exception.Message)"
            if ($attempt -lt $Attempts) {
                Start-Sleep -Seconds ($DelaySeconds * $attempt)
            }
        }
    }

    return $false
}

function Get-OriginUrl {
    $url = (& git config --get remote.origin.url).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($url)) {
        throw "remote.origin.url is not configured"
    }
    return $url
}

function Get-GitHubSsh443Target {
    param([Parameter(Mandatory = $true)][string]$OriginUrl)

    if ($OriginUrl -match "^ssh://git@github\.com/(.+)$") {
        return "git@ssh.github.com:$($Matches[1])"
    }
    if ($OriginUrl -match "^git@github\.com:(.+)$") {
        return "git@ssh.github.com:$($Matches[1])"
    }
    if ($OriginUrl -match "^https://github\.com/(.+)$") {
        return "git@ssh.github.com:$($Matches[1])"
    }

    return $null
}

function Invoke-WithGitSshCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    $oldValue = $env:GIT_SSH_COMMAND
    try {
        $env:GIT_SSH_COMMAND = $Value
        & $Action
    } finally {
        if ($null -eq $oldValue) {
            Remove-Item Env:\GIT_SSH_COMMAND -ErrorAction SilentlyContinue
        } else {
            $env:GIT_SSH_COMMAND = $oldValue
        }
    }
}

function Invoke-RemoteCommand {
    param([Parameter(Mandatory = $true)][string]$Command)

    Invoke-CheckedCommand -File "ssh" -Arguments @($Server, $Command)
}

function Assert-CleanTrackedWorktree {
    $status = (& git status --short --untracked-files=no)
    if ($LASTEXITCODE -ne 0) {
        throw "git status failed"
    }
    if ($status) {
        throw "tracked working tree is dirty; commit or revert changes before syncing"
    }
}

function Push-GitHub {
    if ($SkipGithub) {
        Write-Warning "Skipping GitHub push because -SkipGithub was provided"
        return $false
    }

    $originUrl = Get-OriginUrl
    $ssh443Target = Get-GitHubSsh443Target -OriginUrl $originUrl

    $pushed = Invoke-WithRetry -Name "git push origin" -Action {
        Invoke-CheckedCommand -File "git" -Arguments @("push", "origin", $Branch)
    }
    if ($pushed) {
        return $true
    }

    if ($null -eq $ssh443Target) {
        Write-Warning "Cannot derive GitHub SSH-over-443 URL from origin: $originUrl"
        return $false
    }

    return Invoke-WithRetry -Name "git push ssh.github.com:443" -Action {
        Invoke-WithGitSshCommand -Value "ssh -p 443 -o StrictHostKeyChecking=accept-new" -Action {
            Invoke-CheckedCommand -File "git" -Arguments @("push", $ssh443Target, $Branch)
        }
    }
}

function Invoke-ServerDeployFromGitHub {
    $remoteCommand = "cd $ServerRepo && git fetch origin $Branch && git merge --ff-only origin/$Branch && ./scripts/deploy_to_runtime.sh $RuntimeDir && systemctl restart $ServiceName && systemctl is-active $ServiceName && git rev-parse HEAD"
    return Invoke-WithRetry -Name "server deploy from GitHub" -Action {
        Invoke-RemoteCommand -Command $remoteCommand
    }
}

function Invoke-ServerDeployFromBundle {
    if ($NoBundleFallback) {
        Write-Warning "Skipping bundle fallback because -NoBundleFallback was provided"
        return $false
    }

    $localHead = (& git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "git rev-parse HEAD failed"
    }

    $bundleName = "qqbot-sync-$localHead.bundle"
    $bundlePath = Join-Path $env:TEMP $bundleName
    $remoteBundle = "/tmp/$bundleName"

    if (Test-Path $bundlePath) {
        Remove-Item -LiteralPath $bundlePath -Force
    }

    Invoke-CheckedCommand -File "git" -Arguments @("bundle", "create", $bundlePath, $Branch)

    $copied = Invoke-WithRetry -Name "copy bundle to server" -Action {
        Invoke-CheckedCommand -File "scp" -Arguments @($bundlePath, "$Server`:$remoteBundle")
    }
    if (-not $copied) {
        return $false
    }

    $remoteCommand = "cd $ServerRepo && git fetch $remoteBundle $Branch && git merge --ff-only FETCH_HEAD && ./scripts/deploy_to_runtime.sh $RuntimeDir && systemctl restart $ServiceName && systemctl is-active $ServiceName && git rev-parse HEAD"
    return Invoke-WithRetry -Name "server deploy from bundle" -Action {
        Invoke-RemoteCommand -Command $remoteCommand
    }
}

Assert-CleanTrackedWorktree

$head = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "git rev-parse HEAD failed"
}
Write-Host "Syncing $Branch at $head"

$githubPushed = Push-GitHub
if ($githubPushed) {
    $deployed = Invoke-ServerDeployFromGitHub
} else {
    Write-Warning "GitHub push did not complete; using bundle fallback for server deployment"
    $deployed = Invoke-ServerDeployFromBundle
}

if (-not $deployed) {
    throw "server deployment failed"
}

$serverHead = (& ssh $Server "cd $ServerRepo && git rev-parse HEAD").Trim()
if ($LASTEXITCODE -ne 0) {
    throw "failed to verify server HEAD"
}
if ($serverHead -ne $head) {
    throw "server HEAD is $serverHead, expected $head"
}

if (-not $githubPushed) {
    Write-Warning "Server deployment completed, but GitHub is still not synced"
}

Write-Host "Production sync completed for $head"
