# 수신 웹페이지(receiver 폴더)를 VibeDrop 에 배포하고 주소를 receiver_url.txt 에 저장한다.
$ErrorActionPreference = 'Continue'
Set-Location $PSScriptRoot
if (-not (Get-Command npx -ErrorAction SilentlyContinue)) {
    Write-Host 'Node.js(npx)가 필요합니다: https://nodejs.org 에서 LTS 버전을 설치하세요.' -ForegroundColor Red
    exit 1
}
$cliArgs = @('-y', '@vibedrop/cli', 'deploy', 'receiver', '--unlisted', '--title', 'JAB Optical Receiver')
$slug = ''
if (Test-Path 'receiver_slug.txt') { $slug = (Get-Content 'receiver_slug.txt' -Raw).Trim() }
if ($slug) { $cliArgs += @('--slug', $slug); Write-Host "기존 사이트($slug)에 재배포합니다." }
$out = & npx @cliArgs 2>&1 | Out-String
if ($slug -and $out -notmatch 'vibedrop\.site') {
    Write-Host '재배포 실패 → 새 사이트로 배포합니다.'
    $cliArgs = $cliArgs[0..6]
    $out = & npx @cliArgs 2>&1 | Out-String
}
Write-Host $out
$m = [regex]::Match($out, 'https://([a-z0-9-]+)\.vibedrop\.site')
if ($m.Success) {
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'receiver_url.txt'), $m.Value)
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'receiver_slug.txt'), $m.Groups[1].Value)
    Write-Host ''
    Write-Host ('수신 페이지: ' + $m.Value) -ForegroundColor Green
    Write-Host '주소를 receiver_url.txt 에 저장했습니다. 송신 프로그램이 자동으로 읽어 QR로 보여줍니다.'
    Write-Host '위에 출력된 claim 링크로 VibeDrop 계정에 사이트를 귀속시키면 관리할 수 있습니다 (무료: 30일 미사용 시 만료).'
} else {
    Write-Host '배포 주소를 찾지 못했습니다. 위 출력 내용을 확인하세요.' -ForegroundColor Red
}
