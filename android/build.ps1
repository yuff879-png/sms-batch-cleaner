param(
    [string]$SdkRoot = "$env:LOCALAPPDATA\Android\Sdk",
    [string]$JdkRoot = 'C:\Program Files\Java\jdk-21.0.10'
)
$ErrorActionPreference = 'Stop'
$AndroidRoot = $PSScriptRoot
$BuildRoot = Join-Path $AndroidRoot 'build'
$ClassesRoot = Join-Path $BuildRoot 'classes'
$AndroidJar = Join-Path $SdkRoot 'platforms\android-36.1\android.jar'
$D8 = Join-Path $SdkRoot 'build-tools\36.1.0\lib\d8.jar'
$Javac = Join-Path $JdkRoot 'bin\javac.exe'
$Java = Join-Path $JdkRoot 'bin\java.exe'
$Jar = Join-Path $JdkRoot 'bin\jar.exe'
foreach ($Required in @($AndroidJar, $D8, $Javac, $Java, $Jar)) {
    if (!(Test-Path -LiteralPath $Required)) { throw "Build dependency missing: $Required" }
}
New-Item -ItemType Directory -Path $ClassesRoot -Force | Out-Null
& $Javac -encoding UTF-8 --release 8 -classpath $AndroidJar -d $ClassesRoot (Join-Path $AndroidRoot 'SmsBridge.java')
if ($LASTEXITCODE -ne 0) { throw 'Java compilation failed.' }
$ClassesJar = Join-Path $BuildRoot 'classes.jar'
& $Jar --create --file $ClassesJar -C $ClassesRoot .
if ($LASTEXITCODE -ne 0) { throw 'Class archive failed.' }
$BridgeJar = Join-Path $BuildRoot 'bridge.jar'
& $Java -cp $D8 com.android.tools.r8.D8 --release --min-api 29 --lib $AndroidJar --output $BridgeJar $ClassesJar
if ($LASTEXITCODE -ne 0) { throw 'DEX compilation failed.' }
Get-FileHash -LiteralPath $BridgeJar -Algorithm SHA256 | Select-Object Path,Hash
