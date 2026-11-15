import SwiftUI
import AppKit

struct ServiceStatus: Decodable {
    var state: String
    var running: Bool
    var can_stop: Bool
    var kindle_url: String?
    var dashboard_url: String
    var message: String
    var error: String?
}

@MainActor final class ServiceModel: ObservableObject {
    @Published var status = ServiceStatus(state: "checking", running: false, can_stop: false,
        dashboard_url: "http://localhost:8000", message: "正在检查状态")
    @Published var working = false
    @Published var pendingAction: String?
    @Published var copied = false
    @Published var error: String?
    private var timer: Timer?
    private var queuedAction: String?
    let root: URL

    init() {
        let beside = Bundle.main.bundleURL.deletingLastPathComponent()
        if let bundled = AppRuntime.bundledRoot {
            root = bundled
        } else if FileManager.default.fileExists(atPath: beside.appendingPathComponent("kindle_dashboard.py").path) {
            root = beside
        } else {
            let location = Bundle.main.url(forResource: "project", withExtension: "path")
            let path = location.flatMap { try? String(contentsOf: $0, encoding: .utf8) }.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            root = URL(fileURLWithPath: path ?? beside.path)
        }
        run("status")
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.run("status") }
        }
    }
    var title: String {
        switch status.state {
        case "running": return "运行中"
        case "checking": return "检查中"
        case "conflict": return "端口占用"
        case "unresponsive": return "未响应"
        case "unknown": return "状态未知"
        default: return "已停止"
        }
    }
    var displayTitle: String {
        switch pendingAction {
        case "start": return "启动中"
        case "stop": return "停止中"
        case "restart": return "重启中"
        default: return title
        }
    }
    var color: Color { status.running ? .green : ["stopped", "checking"].contains(status.state) ? .secondary : .orange }
    func run(_ action: String) {
        guard !working else {
            if action != "status", pendingAction == nil, queuedAction == nil {
                queuedAction = action
                pendingAction = action
                error = nil
            }
            return
        }
        working = true
        if action != "status" { error = nil; pendingAction = action }
        let project = root
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = AppRuntime.python(for: project)
            process.environment = AppRuntime.environment(for: project)
            process.currentDirectoryURL = project
            process.arguments = ["-m", "dashboard.control", action]
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = FileHandle.nullDevice
            var decoded: ServiceStatus?
            var failure: String?
            do {
                let finished = DispatchSemaphore(value: 0)
                process.terminationHandler = { _ in finished.signal() }
                try process.run()
                DispatchQueue.global().async {
                    if finished.wait(timeout: .now() + 30) == .timedOut, process.isRunning {
                        process.terminate()
                    }
                }
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                decoded = try JSONDecoder().decode(ServiceStatus.self, from: data)
            } catch { failure = "无法读取服务状态，请确认项目文件夹和运行环境仍在原位置。" }
            let result = decoded
            let message = failure
            DispatchQueue.main.async {
                if let result { self.status = result }
                if let message {
                    self.error = message
                    self.status = ServiceStatus(state: "unknown", running: false, can_stop: false,
                        dashboard_url: "http://localhost:8000", message: "无法确认服务状态")
                }
                else if action != "status" { self.error = result?.error }
                self.working = false
                if action != "status" { self.pendingAction = nil }
                if let next = self.queuedAction {
                    self.queuedAction = nil
                    self.run(next)
                }
            }
        }
    }
    func copyAddress() {
        guard let address = status.kindle_url else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(address, forType: .string)
        copied = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.copied = false }
    }
    func openDashboard() {
        if let url = URL(string: status.dashboard_url), status.running { NSWorkspace.shared.open(url) }
    }
    func openLog() { NSWorkspace.shared.open(AppRuntime.dataDirectory(for: root).appendingPathComponent("server.log")) }
    func openGuide() {
        if let guide = Bundle.main.url(forResource: "Usage", withExtension: "txt") {
            NSWorkspace.shared.open(guide)
        }
    }
}

private enum AppArtwork {
    static func load() -> NSImage {
        if let url = Bundle.main.url(forResource: "AppMark", withExtension: "png"),
           let data = try? Data(contentsOf: url), let icon = NSImage(data: data) { return icon }
        return NSApplication.shared.applicationIconImage
    }
}

struct ControlView: View {
    @State private var accessPresented = false
    @EnvironmentObject var model: ServiceModel
    private var changing: Bool { model.pendingAction != nil }

    var body: some View {
        VStack(spacing: 0) {
            header
                .padding(.horizontal, 24).padding(.vertical, 20)
            Divider()
            VStack(alignment: .leading, spacing: 12) {
                Text("Kindle 访问地址")
                    .font(.system(size: 13, weight: .medium)).foregroundStyle(.secondary)
                HStack(spacing: 12) {
                    Text(model.status.kindle_url ?? "等待网络连接")
                        .font(.system(size: 14, weight: .medium, design: .monospaced))
                        .foregroundStyle(model.status.running ? .primary : .secondary)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    Button { model.copyAddress() } label: {
                        Image(systemName: model.copied ? "checkmark" : "doc.on.doc")
                            .font(.system(size: 14))
                            .frame(width: 28, height: 28)
                    }
                    .buttonStyle(.borderless)
                    .foregroundStyle(model.copied ? Color.green : Color.accentColor)
                    .accessibilityLabel(model.copied ? "已复制" : "复制 Kindle 地址")
                    .help(model.copied ? "已复制" : "复制 Kindle 地址")
                    .disabled(changing || !model.status.running || model.status.kindle_url == nil)
                }
                .padding(.horizontal, 12).padding(.vertical, 10)
                .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.primary.opacity(0.10)))
                if let error = model.error {
                    notice(error)
                } else if ["conflict", "unresponsive", "unknown"].contains(model.status.state) {
                    notice(model.status.message)
                }
            }
            .padding(24).frame(maxWidth: .infinity, alignment: .leading)
            Divider()
            HStack(spacing: 10) {
                Button {
                    if model.status.running { model.openDashboard() }
                    else { model.run("start") }
                } label: {
                    Label(model.status.running ? "打开电脑看板" : "启动看板",
                          systemImage: model.status.running ? "arrow.up.right" : "power")
                        .font(.system(size: 13, weight: .medium))
                        .padding(.horizontal, 6).padding(.vertical, 3)
                }
                .buttonStyle(.borderedProminent)
                .disabled(changing || (!model.status.running && model.status.state != "stopped"))
                Spacer(minLength: 8)
                Button("访问管理") { accessPresented = true }
                    .buttonStyle(.bordered)
                Button("停止服务") { model.run("stop") }
                    .buttonStyle(.bordered)
                    .disabled(changing || !model.status.can_stop)
            }
            .controlSize(.regular)
            .padding(.horizontal, 24).padding(.vertical, 16)
            .background(Color.primary.opacity(0.025))
        }
        .frame(width: 440)
        .sheet(isPresented: $accessPresented) { AccessView(root: model.root) }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(nsImage: AppArtwork.load())
                .resizable().frame(width: 36, height: 36).accessibilityHidden(true)
            Text("Kindle 看板").font(.system(size: 17, weight: .semibold))
            Spacer(minLength: 12)
            HStack(spacing: 6) {
                if changing { ProgressView().controlSize(.mini) }
                else { Circle().fill(model.color).frame(width: 6, height: 6) }
                Text(model.displayTitle)
                    .font(.system(size: 12, weight: .medium)).foregroundStyle(.secondary)
            }
            Menu {
                Button("重新启动") { model.run("restart") }.disabled(changing || !model.status.can_stop)
                Divider()
                Button("使用说明") { model.openGuide() }
            } label: {
                Image(systemName: "ellipsis").frame(width: 24, height: 24)
            }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().help("更多")
        }
    }

    private func notice(_ message: String) -> some View {
        Label(message, systemImage: "exclamationmark.circle")
            .font(.system(size: 12)).foregroundStyle(.orange)
            .fixedSize(horizontal: false, vertical: true)
    }
}

class ApplicationDelegate: NSObject, NSApplicationDelegate {
    private var menuObserver: NSObjectProtocol?
    private var cleanupScheduled = false
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.applicationIconImage = AppArtwork.load()
        menuObserver = NotificationCenter.default.addObserver(forName: NSMenu.didAddItemNotification,
            object: nil, queue: .main) { [weak self] _ in self?.scheduleMenuCleanup() }
        DispatchQueue.main.async {
            NSApp.applicationIconImage = AppArtwork.load()
            self.removeUnusedMenus()
        }
    }
    func applicationDidUpdate(_ notification: Notification) {
        scheduleMenuCleanup()
    }
    private func scheduleMenuCleanup() {
        guard !cleanupScheduled else { return }
        cleanupScheduled = true
        // Defer until SwiftUI has finished its command update, including changes
        // after starting/stopping a service. Do not mutate a menu mid-insertion.
        DispatchQueue.main.async {
            self.cleanupScheduled = false
            self.removeUnusedMenus()
        }
    }
    private func removeUnusedMenus() {
        // SwiftUI adds app-level editing/window menus even after scene commands
        // are replaced. This control utility provides its own Chinese menus.
        guard let menu=NSApp.mainMenu else { return }
        for item in menu.items where ["File", "Edit", "View", "Window", "Help"].contains(item.title) {
            menu.removeItem(item)
        }
    }
}

@main struct KindleControlApp: App {
    @NSApplicationDelegateAdaptor(ApplicationDelegate.self) private var applicationDelegate
    @StateObject private var model = ServiceModel()
    @Environment(\.openWindow) private var openWindow
    var body: some Scene {
        Window("Kindle 看板", id: "control") {
            ControlView().environmentObject(model)
        }.windowResizability(.contentSize)
        .commandsReplaced {
            CommandGroup(replacing: .appInfo) {
                Button("关于 Kindle 看板") { NSApp.orderFrontStandardAboutPanel(nil) }
            }
            CommandGroup(replacing: .appSettings) { }
            CommandGroup(replacing: .systemServices) { }
            CommandGroup(replacing: .appVisibility) {
                Button("隐藏 Kindle 看板") { NSApp.hide(nil) }.keyboardShortcut("h")
                Button("隐藏其他应用") { NSApp.hideOtherApplications(nil) }
                    .keyboardShortcut("h", modifiers: [.command, .option])
                Button("全部显示") { NSApp.unhideAllApplications(nil) }
            }
            CommandGroup(replacing: .appTermination) {
                Button("退出控制应用（看板继续运行）") { NSApp.terminate(nil) }.keyboardShortcut("q")
            }
            CommandMenu("看板") {
                Text("状态：" + model.displayTitle)
                Divider()
                Button("打开控制窗口") {
                    openWindow(id: "control")
                    NSApp.activate(ignoringOtherApps: true)
                }.keyboardShortcut("1")
                Button("打开电脑看板") { model.openDashboard() }
                    .keyboardShortcut("o").disabled(!model.status.running)
                Button("复制 Kindle 地址") { model.copyAddress() }
                    .keyboardShortcut("c", modifiers: [.command, .shift])
                    .disabled(!model.status.running || model.status.kindle_url == nil)
                Divider()
                if model.status.state == "stopped" {
                    Button("启动看板") { model.run("start") }.disabled(model.pendingAction != nil)
                } else {
                    Button("停止看板") { model.run("stop") }
                        .disabled(model.pendingAction != nil || !model.status.can_stop)
                }
                Button("重新启动") { model.run("restart") }
                    .disabled(model.pendingAction != nil || !model.status.can_stop)
                Button("刷新状态") { model.run("status") }
                    .keyboardShortcut("r").disabled(model.working)
            }
            CommandMenu("窗口") {
                Button("显示控制窗口") {
                    openWindow(id: "control")
                    NSApp.activate(ignoringOtherApps: true)
                }
                Divider()
                Button("最小化") { NSApp.keyWindow?.miniaturize(nil) }.keyboardShortcut("m")
                Button("关闭窗口") { NSApp.keyWindow?.performClose(nil) }.keyboardShortcut("w")
            }
            CommandMenu("帮助") {
                Button("使用说明") { model.openGuide() }
                Button("查看运行日志") { model.openLog() }
            }
        }
        MenuBarExtra {
            Text("看板：" + model.title)
            Button("打开控制窗口") {
                openWindow(id: "control")
                NSApp.activate(ignoringOtherApps: true)
            }
            Divider()
            Button("启动看板") { model.run("start") }.disabled(model.working || model.status.state != "stopped")
            Button("停止看板") { model.run("stop") }.disabled(model.working || !model.status.can_stop)
            Button("打开电脑看板") { model.openDashboard() }.disabled(!model.status.running)
            Button("复制 Kindle 地址") { model.copyAddress() }.disabled(!model.status.running || model.status.kindle_url == nil)
            Divider()
            Button("退出控制应用（看板继续运行）") { NSApp.terminate(nil) }
        } label: {
            Image(systemName: model.status.running ? "display" : "display.trianglebadge.exclamationmark")
            Text("看板 · " + model.title)
        }
    }
}
