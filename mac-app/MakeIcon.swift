import AppKit
// Approved direction A: a warm, softly raised usage dial.
let folder=URL(fileURLWithPath:CommandLine.arguments[1])
try FileManager.default.createDirectory(at:folder,withIntermediateDirectories:true)
func color(_ r:CGFloat,_ g:CGFloat,_ b:CGFloat)->NSColor { NSColor(calibratedRed:r,green:g,blue:b,alpha:1) }
func rounded(_ rect:NSRect,_ radius:CGFloat)->NSBezierPath { NSBezierPath(roundedRect:rect,xRadius:radius,yRadius:radius) }
func render(_ pixels:Int)->NSBitmapImageRep {
    let bitmap=NSBitmapImageRep(bitmapDataPlanes:nil,pixelsWide:pixels,pixelsHigh:pixels,bitsPerSample:8,samplesPerPixel:4,hasAlpha:true,isPlanar:false,colorSpaceName:.deviceRGB,bytesPerRow:0,bitsPerPixel:0)!
    NSGraphicsContext.saveGraphicsState();NSGraphicsContext.current=NSGraphicsContext(bitmapImageRep:bitmap)
    let transform=NSAffineTransform();transform.scale(by:CGFloat(pixels)/1024);transform.concat()
    let tile=rounded(NSRect(x:14,y:14,width:996,height:996),214)
    NSGraphicsContext.saveGraphicsState();tile.addClip()
    let gradient=NSGradient(starting:color(0.99,0.99,0.96),ending:color(0.86,0.89,0.85))!
    gradient.draw(in:NSRect(x:0,y:0,width:1024,height:1024),angle:270)
    NSGraphicsContext.restoreGraphicsState()
        // An elevated dial, with a single arc and needle at Dock size.
        let dial=NSBezierPath(ovalIn:NSRect(x:190,y:190,width:644,height:644))
        let shadow=NSShadow();shadow.shadowColor=NSColor.black.withAlphaComponent(0.16);shadow.shadowBlurRadius=32;shadow.shadowOffset=NSSize(width:0,height:-18)
        NSGraphicsContext.saveGraphicsState();shadow.set();color(0.98,0.98,0.95).setFill();dial.fill();NSGraphicsContext.restoreGraphicsState()
        NSColor.white.withAlphaComponent(0.85).setStroke();dial.lineWidth=3;dial.stroke()
        let track=NSBezierPath();track.appendArc(withCenter:NSPoint(x:512,y:508),radius:221,startAngle:220,endAngle:-40,clockwise:true)
        color(0.86,0.89,0.84).setStroke();track.lineWidth=49;track.lineCapStyle = .round;track.stroke()
        let arc=NSBezierPath();arc.appendArc(withCenter:NSPoint(x:512,y:508),radius:221,startAngle:220,endAngle:40,clockwise:true)
        color(0.20,0.39,0.32).setStroke();arc.lineWidth=49;arc.lineCapStyle = .round;arc.stroke()
        let needle=NSBezierPath();needle.move(to:NSPoint(x:512,y:508));needle.line(to:NSPoint(x:643,y:627));needle.lineWidth=26;needle.lineCapStyle = .round;color(0.12,0.22,0.18).setStroke();needle.stroke()
        color(0.12,0.22,0.18).setFill();NSBezierPath(ovalIn:NSRect(x:482,y:478,width:60,height:60)).fill()
        let base=rounded(NSRect(x:448,y:351,width:128,height:22),11);color(0.71,0.76,0.70).setFill();base.fill()

    NSGraphicsContext.restoreGraphicsState()
    return bitmap
}
for size in [16,32,128,256,512] {
    for scale in [1,2] {
        let bitmap=render(size*scale)
        let name="icon_\(size)x\(size)"+(scale==2 ? "@2x" : "")+".png"
        try bitmap.representation(using:.png,properties:[:])!.write(to:folder.appendingPathComponent(name))
    }
}
