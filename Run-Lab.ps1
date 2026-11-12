[CmdletBinding()]
param(
    [ValidateSet('Setup', 'Drills', 'Benchmark')]
    [string]$Stage = 'Setup',
    [string]$Workspace = (Join-Path $PSScriptRoot '.lab-work'),
    [string]$Evidence = (Join-Path $PSScriptRoot 'evidence-rerun'),
    [ValidateRange(1, 1440)]
    [int]$Minutes = 60,
    [ValidateRange(1, 5)]
    [int]$Repetitions = 5,
    [ValidateRange(1, 2147483647)]
    [int]$HostCount = 500
)

$ErrorActionPreference = 'Stop'
$Workspace = [IO.Path]::GetFullPath($Workspace)
$Evidence = [IO.Path]::GetFullPath($Evidence)
$runtime = Join-Path $Workspace 'runtime'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'

function Receive-Asset($Url, $Name, $Hash) {
    $destination = Join-Path $runtime $Name
    if (!(Test-Path -LiteralPath $destination)) {
        $partial = "$destination.partial"
        & curl.exe -fLsS --retry 2 $Url -o $partial
        if ($LASTEXITCODE -ne 0) { throw "Download failed for $Name." }
        if ((Get-FileHash -LiteralPath $partial).Hash -ne $Hash) {
            throw "SHA-256 mismatch for $Name."
        }
        Move-Item -LiteralPath $partial -Destination $destination -Force
    }
    if ((Get-FileHash -LiteralPath $destination).Hash -ne $Hash) {
        throw "SHA-256 mismatch for existing asset $Name."
    }
    return $destination
}

function Invoke-Checked($Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable exited with status $LASTEXITCODE." }
}

if ($Stage -eq 'Setup') {
    New-Item -ItemType Directory -Force $runtime | Out-Null
    $sevenZip = Get-Command 7z.exe -ErrorAction SilentlyContinue
    $extractor = if ($sevenZip) { $sevenZip.Source } else { Join-Path $env:ProgramFiles '7-Zip/7z.exe' }
    if (!(Test-Path -LiteralPath $extractor)) {
        throw '7-Zip is required to extract the QEMU distribution.'
    }

    $prometheus = Receive-Asset 'https://github.com/prometheus/prometheus/releases/download/v3.16.0/prometheus-3.16.0.windows-amd64.zip' 'prom.zip' '89c03c2f24400fb58668440b6d7226f68564acda27d657e77acfbda42be32cb3'
    $alertmanager = Receive-Asset 'https://github.com/prometheus/alertmanager/releases/download/v0.35.0/alertmanager-0.35.0.windows-amd64.zip' 'am.zip' 'cdf8cd664889bf3f27771881e10814f3d5916310aba90717b4424deb358b0fa3'
    $grafana = Receive-Asset 'https://dl.grafana.com/grafana/release/13.1.2/grafana_13.1.2_30900078095_windows_amd64.tar.gz' 'grafana-13.1.2.tar.gz' '7babb896011259fc58fac920b67819b4bac22ad108dadd2db9b40ca550cd7e08'
    $qemu = Receive-Asset 'https://qemu.eu/w64/2026/qemu-w64-setup-20260805.exe' 'qemu.exe' '9d39b0289d879cc26dc1c17dfaa8a6c3acb68329fb1d38f8f9befc4bbf2d11d4'
    $alpine = 'https://dl-cdn.alpinelinux.org/alpine/v3.24'
    $kernel = Receive-Asset "$alpine/releases/x86_64/netboot-3.24.4/vmlinuz-virt" 'vmlinuz' '1e8ac7aebfab3f0fcb443eae94ad320efbbda51be91b928c32ca1740b44ebe9d'
    $baseInit = Receive-Asset "$alpine/releases/x86_64/netboot-3.24.4/initramfs-virt" 'alpine-initramfs' 'cf39ceb8bd232584851218bf827668e07818cba82aeea784b1cc03969d01c3a5'
    $static = Receive-Asset "$alpine/main/x86_64/busybox-static-1.37.0-r31.apk" 'busybox.apk' 'b0d6cfc585d7fdd12df359cc5d38d5e45894e9e9e78c4059eb41577d022ec734'
    $extras = Receive-Asset "$alpine/main/x86_64/busybox-extras-1.37.0-r31.apk" 'busybox-extras.apk' '3183ea63aa3b663e3875fecb92438d1d08364b6bba2df8180399ddcf1c0455fb'

    Expand-Archive -Path $prometheus -DestinationPath (Join-Path $runtime 'prom') -Force
    Expand-Archive -Path $alertmanager -DestinationPath (Join-Path $runtime 'am') -Force
    foreach ($directory in 'grafana', 'qemu', 'guest') {
        New-Item -ItemType Directory -Force (Join-Path $runtime $directory) | Out-Null
    }
    Invoke-Checked tar.exe @('-xf', $grafana, '-C', (Join-Path $runtime 'grafana'))
    Invoke-Checked $extractor @('x', '-y', "-o$(Join-Path $runtime 'qemu')", $qemu)
    Invoke-Checked tar.exe @('-xf', $static, '-C', (Join-Path $runtime 'guest'), 'bin/busybox.static')
    Invoke-Checked tar.exe @('-xf', $extras, '-C', (Join-Path $runtime 'guest'), 'bin/busybox-extras')
    Invoke-Checked python @('-B', (Join-Path $PSScriptRoot 'build_guest.py'), $baseInit,
        (Join-Path $runtime 'guest/bin/busybox.static'), (Join-Path $runtime 'guest/bin/busybox-extras'),
        (Join-Path $runtime 'lab-initramfs'))
    return
}

$promExe = (Get-ChildItem (Join-Path $runtime 'prom') -Recurse -Filter prometheus.exe | Select-Object -First 1).FullName
$evidencePath = Join-Path $Evidence "$Stage-$stamp"
if ($Stage -eq 'Benchmark') {
    Invoke-Checked python @('-B', (Join-Path $PSScriptRoot 'benchmark.py'),
        '--prometheus', $promExe, '--state', (Join-Path $Workspace "benchmark-$stamp"),
        '--output', $evidencePath, '--minutes', "$Minutes", '--hosts', "$HostCount", '--hardware-events')
    return
}

$alertExe = (Get-ChildItem (Join-Path $runtime 'am') -Recurse -Filter alertmanager.exe | Select-Object -First 1).FullName
$grafExe = Join-Path $runtime 'grafana/grafana-13.1.2/bin/grafana.exe'
Invoke-Checked python @('-B', (Join-Path $PSScriptRoot 'drills.py'),
    '--qemu', (Join-Path $runtime 'qemu/qemu-system-x86_64.exe'),
    '--kernel', (Join-Path $runtime 'vmlinuz'),
    '--initrd', (Join-Path $runtime 'lab-initramfs'),
    '--prometheus', $promExe, '--alertmanager', $alertExe, '--grafana', $grafExe,
    '--state', (Join-Path $Workspace "drills-$stamp"),
    '--output', $evidencePath, '--repetitions', "$Repetitions")
