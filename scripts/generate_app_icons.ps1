param(
    [string]$SourceImage = ""
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir
$StaticDir = Join-Path $RootDir "ui\static"
$ResDir = Join-Path $RootDir "mobile\android\app\src\main\res"

$MasterPng = Join-Path $StaticDir "icon-512.png"
if ([string]::IsNullOrWhiteSpace($SourceImage)) {
    $SourceImage = $MasterPng
}
if (-not (Test-Path $SourceImage)) {
    throw "Source image not found: $SourceImage"
}

$srcBytes = [System.IO.File]::ReadAllBytes($SourceImage)
$ms = New-Object System.IO.MemoryStream(, $srcBytes)
$srcBmp = [System.Drawing.Bitmap]::FromStream($ms)

try {
    # Sample obsidian navy background pixel near corner
    $sx = [Math]::Min(4, $srcBmp.Width - 1)
    $sy = [Math]::Min(4, $srcBmp.Height - 1)
    $bgColor = $srcBmp.GetPixel($sx, $sy)
    $bgHex = "#{0:X2}{1:X2}{2:X2}" -f $bgColor.R, $bgColor.G, $bgColor.B

    function New-HiResGraphics([System.Drawing.Bitmap]$bmp) {
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        return $g
    }

    function Add-RoundedRectPath([float]$x, [float]$y, [float]$w, [float]$h, [float]$r) {
        $d = $r * 2
        $path = New-Object System.Drawing.Drawing2D.GraphicsPath
        $path.AddArc($x, $y, $d, $d, 180, 90)
        $path.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
        $path.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
        $path.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
        $path.CloseFigure()
        return $path
    }

    function Save-CroppedSquare([System.Drawing.Bitmap]$src, [string]$outPath, [int]$size, [double]$zoom, [double]$cornerRatio, [bool]$circleMask, [System.Drawing.Color]$fillBg) {
        $dir = Split-Path -Parent $outPath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

        $bmp = New-Object System.Drawing.Bitmap($size, $size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $g = New-HiResGraphics $bmp
        try {
            if ($circleMask -or $cornerRatio -gt 0) {
                $g.Clear([System.Drawing.Color]::Transparent)
            } else {
                $g.Clear($fillBg)
            }

            $clipPath = $null
            if ($circleMask) {
                $clipPath = New-Object System.Drawing.Drawing2D.GraphicsPath
                $clipPath.AddEllipse(0, 0, $size, $size)
                $g.SetClip($clipPath)
            } elseif ($cornerRatio -gt 0) {
                $clipPath = Add-RoundedRectPath 0 0 $size $size ($size * $cornerRatio)
                $g.SetClip($clipPath)
            }

            $side = [Math]::Min($src.Width, $src.Height)
            $cropSide = [float]($side / $zoom)
            $cx = [float](($src.Width - $cropSide) / 2.0)
            $cy = [float](($src.Height - $cropSide) / 2.0)
            $destRect = New-Object System.Drawing.RectangleF(0, 0, $size, $size)
            $srcRect = New-Object System.Drawing.RectangleF($cx, $cy, $cropSide, $cropSide)
            $g.DrawImage($src, $destRect, $srcRect, [System.Drawing.GraphicsUnit]::Pixel)

            if ($clipPath -ne $null) { $clipPath.Dispose() }
        } finally {
            $g.Dispose()
        }
        $bmp.Save($outPath, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
    }

    function Save-AdaptiveForeground([System.Drawing.Bitmap]$src, [string]$outPath, [int]$size, [System.Drawing.Color]$fillBg) {
        $dir = Split-Path -Parent $outPath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

        $bmp = New-Object System.Drawing.Bitmap($size, $size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $g = New-HiResGraphics $bmp
        try {
            $g.Clear($fillBg)
            $inner = [int][Math]::Round($size * 0.94)
            $pad = [int][Math]::Round(($size - $inner) / 2.0)
            $g.DrawImage($src, $pad, $pad, $inner, $inner)
        } finally {
            $g.Dispose()
        }
        $bmp.Save($outPath, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
    }

    function Save-Splash([System.Drawing.Bitmap]$src, [string]$outPath, [int]$w, [int]$h, [System.Drawing.Color]$fillBg) {
        $dir = Split-Path -Parent $outPath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

        $bmp = New-Object System.Drawing.Bitmap($w, $h, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $g = New-HiResGraphics $bmp
        try {
            $g.Clear($fillBg)
            $logoSize = [int][Math]::Round([Math]::Min($w, $h) * 0.34)
            $ox = [int][Math]::Round(($w - $logoSize) / 2.0)
            $oy = [int][Math]::Round(($h - $logoSize) / 2.0)
            $g.DrawImage($src, $ox, $oy, $logoSize, $logoSize)
        } finally {
            $g.Dispose()
        }
        $bmp.Save($outPath, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
    }

    # 1. Web & PWA Icons (<= 512px)
    Save-CroppedSquare $srcBmp (Join-Path $StaticDir "favicon.png") 64 1.12 0.18 $false $bgColor
    Save-CroppedSquare $srcBmp (Join-Path $StaticDir "apple-touch-icon.png") 180 1.08 0.0 $false $bgColor
    Save-CroppedSquare $srcBmp (Join-Path $StaticDir "icon-192.png") 192 1.10 0.0 $false $bgColor
    Save-CroppedSquare $srcBmp (Join-Path $StaticDir "icon-512.png") 512 1.10 0.0 $false $bgColor
    Save-CroppedSquare $srcBmp (Join-Path $StaticDir "icon-maskable-512.png") 512 1.0 0.0 $false $bgColor

    # 3. Android Mipmap Densities
    $mipmaps = @(
        @{ Folder = "mipmap-mdpi";    Legacy = 48;  Fg = 108 },
        @{ Folder = "mipmap-hdpi";    Legacy = 72;  Fg = 162 },
        @{ Folder = "mipmap-xhdpi";   Legacy = 96;  Fg = 216 },
        @{ Folder = "mipmap-xxhdpi";  Legacy = 144; Fg = 324 },
        @{ Folder = "mipmap-xxxhdpi"; Legacy = 192; Fg = 432 }
    )
    foreach ($m in $mipmaps) {
        $dir = Join-Path $ResDir $m.Folder
        Save-CroppedSquare $srcBmp (Join-Path $dir "ic_launcher.png") $m.Legacy 1.12 0.18 $false $bgColor
        Save-CroppedSquare $srcBmp (Join-Path $dir "ic_launcher_round.png") $m.Legacy 1.12 0.0 $true $bgColor
        Save-AdaptiveForeground $srcBmp (Join-Path $dir "ic_launcher_foreground.png") $m.Fg $bgColor
    }

    # 4. Android Splash Screens (Obsidian Dark + Centered Logo)
    $splashes = @(
        @{ Path = "drawable\splash.png";               W = 480;  H = 320 },
        @{ Path = "drawable-land-mdpi\splash.png";     W = 480;  H = 320 },
        @{ Path = "drawable-land-hdpi\splash.png";     W = 800;  H = 480 },
        @{ Path = "drawable-land-xhdpi\splash.png";    W = 1280; H = 720 },
        @{ Path = "drawable-land-xxhdpi\splash.png";   W = 1600; H = 960 },
        @{ Path = "drawable-land-xxxhdpi\splash.png";  W = 1920; H = 1280 },
        @{ Path = "drawable-port-mdpi\splash.png";     W = 320;  H = 480 },
        @{ Path = "drawable-port-hdpi\splash.png";     W = 480;  H = 800 },
        @{ Path = "drawable-port-xhdpi\splash.png";    W = 720;  H = 1280 },
        @{ Path = "drawable-port-xxhdpi\splash.png";   W = 960;  H = 1600 },
        @{ Path = "drawable-port-xxxhdpi\splash.png";  W = 1280; H = 1920 }
    )
    foreach ($s in $splashes) {
        Save-Splash $srcBmp (Join-Path $ResDir $s.Path) $s.W $s.H $bgColor
    }

    # 5. Adaptive Icon Background Color XML matched to sampled Obsidian corner
    $bgXml = @"
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <color name="ic_launcher_background">$bgHex</color>
</resources>
"@
    [System.IO.File]::WriteAllText((Join-Path $ResDir "values\ic_launcher_background.xml"), $bgXml.Trim(), [System.Text.UTF8Encoding]::new($false))

    Write-Output "Generated AutoORB icons & splashes (sampled bg: $bgHex)"
} finally {
    $srcBmp.Dispose()
    $ms.Dispose()
}
