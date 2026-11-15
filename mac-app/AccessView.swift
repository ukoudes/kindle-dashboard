import SwiftUI
import AppKit

private struct Viewer: Decodable, Identifiable {
    let id: String
    let name: String
    let browser: String
    let ip: String
    let last_seen: Double
}
private struct Pairing: Decodable {
    let code: String
    let expires_at: Double
}
private struct DeniedVisit: Decodable {
    let ip: String
    let last_seen: Double
}
private struct AccessSnapshot: Decodable {
    var devices: [Viewer] = []
    var pairing: Pairing?
    var denied: [DeniedVisit] = []
    var error: String?

    init() { }
    private enum CodingKeys: String, CodingKey { case devices, pairing, denied, error }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        error = try values.decodeIfPresent(String.self, forKey: .error)
        if error == nil {
            devices = try values.decode([Viewer].self, forKey: .devices)
            pairing = try values.decodeIfPresent(Pairing.self, forKey: .pairing)
            denied = try values.decode([DeniedVisit].self, forKey: .denied)
        }
    }
}

@MainActor private final class AccessModel: ObservableObject {
    @Published var snapshot = AccessSnapshot()
    @Published var working = false
    @Published var error: String?
    let root: URL
    private var timer: Timer?
    init(root: URL) { self.root = root }
    func start() {
        run("status")
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.run("status") }
        }
    }
    func stop() { timer?.invalidate(); timer = nil }
    func run(_ action: String, id: String? = nil) {
        guard !working else { return }
        working = true
        let project = root
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = AppRuntime.python(for: project)
            process.environment = AppRuntime.environment(for: project)
            process.currentDirectoryURL = project
            process.arguments = ["-m", "dashboard.access", action] + (id.map { [$0] } ?? [])
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = FileHandle.nullDevice
            var result: AccessSnapshot?
            do {
                let finished = DispatchSemaphore(value: 0)
                process.terminationHandler = { _ in finished.signal() }
                try process.run()
                DispatchQueue.global().async {
                    if finished.wait(timeout: .now() + 10) == .timedOut, process.isRunning { process.terminate() }
                }
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                result = try JSONDecoder().decode(AccessSnapshot.self, from: data)
            } catch { }
            let decoded = result
            DispatchQueue.main.async {
                self.working = false
                if let decoded, decoded.error == nil {
                    self.snapshot = decoded
                    self.error = nil
                } else {
                    self.error = decoded?.error ?? "暂时无法读取访问权限，请稍后重试。"
                }
            }
        }
    }
}

struct AccessView: View {
    @StateObject private var model: AccessModel
    @Environment(\.dismiss) private var dismiss
    @State private var resetRequested = false
    @State private var copied = false
    init(root: URL) { _model = StateObject(wrappedValue: AccessModel(root: root)) }

    private func elapsed(_ timestamp: Double) -> String {
        let seconds = max(0, Date().timeIntervalSince1970 - timestamp)
        if seconds < 60 { return "刚刚" }
        if seconds < 3600 { return "\(Int(seconds / 60)) 分钟前" }
        if seconds < 86400 { return "\(Int(seconds / 3600)) 小时前" }
        return "\(Int(seconds / 86400)) 天前"
    }

    private var pairingCard: some View {
        Group {
            if let pairing = model.snapshot.pairing {
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    let remaining = max(0, Int(ceil(pairing.expires_at - context.date.timeIntervalSince1970)))
                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            Text("一次性配对码").font(.system(size: 13, weight: .medium))
                            Spacer()
                            Text(remaining > 0 ? String(format: "剩余 %02d:%02d", remaining / 60, remaining % 60) : "已过期")
                                .font(.system(size: 12)).monospacedDigit().foregroundStyle(.secondary)
                        }
                        HStack(spacing: 12) {
                            Text(String(pairing.code.prefix(5)) + " " + String(pairing.code.suffix(5)))
                                .font(.system(size: 23, weight: .medium, design: .monospaced))
                                .foregroundStyle(remaining > 0 ? .primary : .secondary)
                                .textSelection(.enabled)
                                .help("在要查看看板的浏览器中输入此码。使用一次后失效。")
                            Spacer(minLength: 0)
                            Button {
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.setString(pairing.code, forType: .string)
                                copied = true
                            } label: {
                                Image(systemName: copied ? "checkmark" : "doc.on.doc")
                                    .frame(width: 24, height: 24)
                            }
                            .buttonStyle(.borderless)
                            .accessibilityLabel(copied ? "已复制配对码" : "复制配对码")
                            .help(copied ? "已复制" : "复制配对码")
                            .disabled(remaining == 0)
                            Button("取消") { model.run("cancel") }.disabled(model.working)
                                .help("使当前配对码失效")
                        }
                    }
                    .padding(16)
                    .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 6))
                }
            }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("访问管理").font(.system(size: 17, weight: .semibold))
                Spacer()
                Button("完成") { dismiss() }.keyboardShortcut(.cancelAction)
            }.padding(.horizontal, 24).padding(.vertical, 20)
            Divider()
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Text("已授权浏览器").font(.system(size: 13, weight: .semibold))
                    Spacer()
                    Button("生成配对码") { copied = false; model.run("create") }
                        .disabled(model.working || model.snapshot.devices.count >= 8)
                        .help(model.snapshot.devices.count >= 8 ? "最多授权 8 个浏览器，请先撤销不再使用的授权。" : "生成一次性配对码；原有配对码将失效。")
                }
                pairingCard
                if model.snapshot.devices.isEmpty {
                    Text("暂无授权浏览器")
                        .font(.system(size: 13)).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, minHeight: 52)
                } else {
                    ScrollView {
                        VStack(spacing: 0) {
                            ForEach(model.snapshot.devices) { device in
                                HStack(spacing: 10) {
                                    Image(systemName: device.browser == "Kindle" ? "book.closed" : "display")
                                        .font(.system(size: 17)).foregroundStyle(.secondary).frame(width: 24)
                                    Text(device.name).font(.system(size: 13, weight: .medium)).lineLimit(1)
                                        .help(device.name + "\n浏览器：" + device.browser + "\n最近来源 IP：" + device.ip + "（可能属于转发设备）")
                                    Spacer(minLength: 8)
                                    Text(elapsed(device.last_seen) + "访问")
                                        .font(.system(size: 12)).foregroundStyle(.secondary).fixedSize()
                                    Button("撤销") { model.run("revoke", id: device.id) }
                                        .disabled(model.working)
                                        .accessibilityLabel("撤销 " + device.name + " 的访问权限")
                                        .help("撤销此浏览器的查看权限")
                                }
                                .padding(.vertical, 11)
                                Divider()
                            }
                        }
                    }.frame(height: min(CGFloat(model.snapshot.devices.count) * 48, 240))
                }
                if !model.snapshot.denied.isEmpty {
                    DisclosureGroup("近期未授权请求") {
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(model.snapshot.denied.prefix(5), id: \.ip) { visit in
                                HStack {
                                    Text(visit.ip).font(.system(size: 12, design: .monospaced))
                                    Spacer()
                                    Text(elapsed(visit.last_seen)).foregroundStyle(.secondary)
                                }
                            }
                        }.font(.system(size: 12)).padding(.top, 12)
                    }
                    .font(.system(size: 13))
                    .help("最近 24 小时内的未授权请求，最多显示 5 个来源。IP 可能属于转发设备。")
                }
                if let error = model.error {
                    Text(error).font(.system(size: 12)).foregroundStyle(.orange)
                }
            }.padding(24)
            Divider()
            HStack(spacing: 16) {
                Label("连接未加密", systemImage: "lock.open")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
                    .help("当前使用 HTTP。配对限制查看权限，不加密网络传输。")
                Spacer(minLength: 0)
                Button("撤销全部授权") { resetRequested = true }
                    .disabled(model.working || model.snapshot.devices.isEmpty)
            }.padding(.horizontal, 24).padding(.vertical, 16)
        }
        .frame(width: 520)
        .onAppear { model.start() }.onDisappear { model.stop() }
        .onChange(of: model.snapshot.pairing?.code) { _ in copied = false }
        .confirmationDialog("撤销所有浏览器的访问权限？", isPresented: $resetRequested, titleVisibility: .visible) {
            Button("撤销全部授权", role: .destructive) { model.run("reset") }
            Button("取消", role: .cancel) { }
        } message: { Text("所有浏览器需要重新配对，当前配对码失效。统计数据会保留。") }
    }
}
