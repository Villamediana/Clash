
$password = ConvertTo-SecureString "4cp0t12017-A" -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential ("root", $password)
Write-Host "Conectando túnel SSH a 103.199.185.54..."
Write-Host "Si pide confirmar fingerprint, escribe 'yes' y presiona Enter"
Write-Host ""
ssh -D 8080 -N root@103.199.185.54
        