[CmdletBinding()]
param([string]$Printer)
Get-Printer -Name $Printer -ErrorAction SilentlyContinue | Get-PrintJob | Select-Object Id, Name, DocumentName, JobStatus, SubmittedTime | ConvertTo-Json -Depth 4
