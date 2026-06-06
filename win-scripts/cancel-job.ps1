[CmdletBinding()]
param(
    [string]$Printer,
    [Parameter(Mandatory=$true)][int]$JobId
)
Remove-PrintJob -PrinterName $Printer -ID $JobId -ErrorAction SilentlyContinue
Get-Printer -Name $Printer -ErrorAction SilentlyContinue | Get-PrintJob | Select-Object Id, Name, JobStatus | ConvertTo-Json -Depth 4
