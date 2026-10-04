while ($true) {
    $proxyUrl = Read-Host "Proxy"
    
    if ($proxyUrl -eq 'exit' -or $proxyUrl -eq 'quit') {
        Write-Host "Bye"
        break
    }
    
    if ([string]::IsNullOrWhiteSpace($proxyUrl)) {
        continue
    }
    
    if ($proxyUrl -notmatch '^https?://') {
        $proxyUrl = "http://$proxyUrl"
    }
    
    Write-Host "Testing..." -NoNewline
    
    try {
        $result = curl.exe -x $proxyUrl -s --max-time 15 https://ip.sb 2>&1
        
        if ($LASTEXITCODE -eq 0 -and $result) {
            Write-Host "`r[OK] IP: $result" -ForegroundColor Green
        } else {
            Write-Host "`r[FAIL] $result" -ForegroundColor Red
        }
    }
    catch {
        Write-Host "`r[ERROR] $_" -ForegroundColor Red
    }
}