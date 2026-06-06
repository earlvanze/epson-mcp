[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Printer,
    [Parameter(Mandatory=$true)][string]$InputFile,
    [string]$JobName = "epson-mcp-job",
    [string]$OutputFile
)
$ErrorActionPreference = "Stop"
$result = [ordered]@{
    ok = $false
    printer = $Printer
    job = $null
    error = $null
}
try {
    if (-not (Get-Printer -Name $Printer -ErrorAction SilentlyContinue)) {
        # Try to add a standard TCP/IP port + driver if it's a raw printer on the network
        $portName = "EPSON_RAW_9100"
        if (-not (Get-PrinterPort -Name $portName -ErrorAction SilentlyContinue)) {
            Add-PrinterPort -Name $portName -PrinterHostAddress "192.168.4.21"
        }
        if (-not (Get-PrinterDriver -Name "Epson" -ErrorAction SilentlyContinue)) {
            # Try installing a generic/text driver
            try {
                Add-PrinterDriver -Name "Generic / Text Only" -ErrorAction Stop
            } catch {
                # fall through
            }
        }
        try {
            Add-Printer -Name $Printer -DriverName "Generic / Text Only" -PortName $portName
        } catch {
            $result.error = "failed to add printer: $_"
            $result | ConvertTo-Json -Depth 4 | Out-File -FilePath $OutputFile -Encoding utf8
            exit 1
        }
    }
    $bytes = [System.IO.File]::ReadAllBytes($InputFile)
    $job = Start-PrintJob -PrinterName $Printer -Document $JobName -InputObject $bytes
    $result.ok = $true
    $result.job = @{ id = $job.Id; name = $JobName }
} catch {
    $result.error = $_.Exception.Message
    $result | ConvertTo-Json -Depth 4 | Out-File -FilePath $OutputFile -Encoding utf8
    exit 1
}
$result | ConvertTo-Json -Depth 4 | Out-File -FilePath $OutputFile -Encoding utf8
