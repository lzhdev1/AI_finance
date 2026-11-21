# CDP screenshot: wait until the page really renders, then capture.
# ASCII only on purpose: Windows PowerShell 5.1 misreads UTF-8 without BOM.
param(
    [string]$Url = "http://localhost:3000/",
    [string]$Out = (Join-Path $PSScriptRoot "..\..\logs\shot.png"),
    [string]$Selector = ".nav",
    [int]$Port = 9333,
    [int]$WaitSeconds = 25,
    [string]$Js = "",
    [string]$Eval = "",
    [int]$Width = 1600,
    [int]$Height = 1200
)

$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
$profile = Join-Path $env:TEMP ("edge_cdp_" + [guid]::NewGuid().ToString('N').Substring(0, 8))

$proc = Start-Process -FilePath $edge -PassThru -ArgumentList @(
    "--headless=new", "--disable-gpu", "--hide-scrollbars",
    "--remote-debugging-port=$Port", "--user-data-dir=$profile",
    "--window-size=$Width,$Height", "about:blank"
)

$ws = $null
try {
    $deadline = (Get-Date).AddSeconds(20)
    $target = $null
    while ((Get-Date) -lt $deadline -and -not $target) {
        Start-Sleep -Milliseconds 400
        try {
            $list = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/list" -TimeoutSec 2
            $target = $list | Where-Object { $_.type -eq "page" } | Select-Object -First 1
        } catch { }
    }
    if (-not $target) { throw "CDP not reachable" }

    $ws = New-Object System.Net.WebSockets.ClientWebSocket
    $ws.ConnectAsync([Uri]$target.webSocketDebuggerUrl, [Threading.CancellationToken]::None).Wait()

    function Send-Cdp($id, $method, $params) {
        $payload = @{ id = $id; method = $method }
        if ($params) { $payload.params = $params }
        $json = $payload | ConvertTo-Json -Depth 8 -Compress
        $bytes = [Text.Encoding]::UTF8.GetBytes($json)
        $segment = New-Object "System.ArraySegment[byte]" -ArgumentList @(,$bytes)
        $ws.SendAsync($segment, [System.Net.WebSockets.WebSocketMessageType]::Text, $true, [Threading.CancellationToken]::None).Wait()
    }

    function Receive-Cdp($timeoutMs = 8000) {
        # A CDP reply may arrive in several websocket frames; keep reading
        # until EndOfMessage, otherwise the JSON gets truncated.
        $stream = New-Object System.IO.MemoryStream
        $buffer = New-Object byte[] 262144
        $cts = New-Object System.Threading.CancellationTokenSource
        $cts.CancelAfter($timeoutMs)
        do {
            $segment = New-Object "System.ArraySegment[byte]" -ArgumentList @(,$buffer)
            $task = $ws.ReceiveAsync($segment, $cts.Token)
            $task.Wait() | Out-Null
            $stream.Write($buffer, 0, $task.Result.Count)
        } while (-not $task.Result.EndOfMessage)
        return [Text.Encoding]::UTF8.GetString($stream.ToArray())
    }

    Send-Cdp 1 "Page.enable" $null
    Send-Cdp 2 "Emulation.setDeviceMetricsOverride" @{ width = $Width; height = $Height; deviceScaleFactor = 1; mobile = $false }
    Send-Cdp 3 "Page.navigate" @{ url = $Url }

    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    $ready = $false
    $probeId = 100
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 1200
        $probeId++
        Send-Cdp $probeId "Runtime.evaluate" @{
            expression = "document.querySelectorAll('$Selector').length + ':' + document.body.innerText.length"
            returnByValue = $true
        }
        try {
            $msg = Receive-Cdp 6000
            $obj = $msg | ConvertFrom-Json
            if ($obj.result.result.value) {
                $parts = ([string]$obj.result.result.value).Split(":")
                $links = [int]$parts[0]
                $chars = [int]$parts[1]
                Write-Output ("probe: nav=$links chars=$chars")
                if ($links -ge 4 -and $chars -gt 800) { $ready = $true; break }
            }
        } catch { }
    }
    Write-Output "ready=$ready"

    if ($Js) {
        Send-Cdp 800 "Runtime.evaluate" @{ expression = $Js; returnByValue = $true }
        Start-Sleep -Seconds 3
        Send-Cdp 801 "Runtime.evaluate" @{
            expression = "document.querySelectorAll('$Selector').length + ':' + document.body.innerText.length"
            returnByValue = $true
        }
        try { Write-Output ("after-js: " + (Receive-Cdp 6000)) } catch { }
    }

    if ($Eval) {
        Send-Cdp 850 "Runtime.evaluate" @{ expression = $Eval; returnByValue = $true }
        for ($i = 0; $i -lt 6; $i++) {
            $msg = Receive-Cdp 6000
            $obj = $msg | ConvertFrom-Json
            if ($obj.id -eq 850) {
                Write-Output ("eval: " + $obj.result.result.value)
                break
            }
        }
    }

    Send-Cdp 900 "Page.captureScreenshot" @{ format = "png"; captureBeyondViewport = $true }
    $png = $null
    for ($i = 0; $i -lt 20; $i++) {
        $msg = Receive-Cdp 8000
        $obj = $msg | ConvertFrom-Json
        if ($obj.id -eq 900 -and $obj.result.data) { $png = $obj.result.data; break }
    }
    if ($png) {
        [IO.File]::WriteAllBytes($Out, [Convert]::FromBase64String($png))
        Write-Output ("saved " + $Out + " " + (Get-Item $Out).Length + " bytes")
    } else {
        Write-Output "no screenshot data"
    }
} finally {
    if ($ws) { try { $ws.Dispose() } catch { } }
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $profile -ErrorAction SilentlyContinue
}
