import Foundation

// Installed builds keep immutable code in the bundle and writable state in Application Support.
enum AppRuntime {
    static var bundledRoot: URL? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let root = resources.appendingPathComponent("backend", isDirectory: true)
        return FileManager.default.fileExists(atPath: root.appendingPathComponent("kindle_dashboard.py").path) ? root : nil
    }

    static func python(for root: URL) -> URL {
        if let resources = Bundle.main.resourceURL, bundledRoot == root {
            return resources.appendingPathComponent("runtime/bin/python3")
        }
        return root.appendingPathComponent(".venv/bin/python")
    }

    static func dataDirectory(for root: URL) -> URL {
        guard bundledRoot == root else { return root.appendingPathComponent("data", isDirectory: true) }
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        return support.appendingPathComponent("Kindle 看板/data", isDirectory: true)
    }

    static func environment(for root: URL) -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        if bundledRoot == root, let resources = Bundle.main.resourceURL {
            environment["KINDLE_DASHBOARD_DATA_DIR"] = dataDirectory(for: root).path
            environment["KINDLE_DASHBOARD_PYTHON"] = python(for: root).path
            environment["PYTHONHOME"] = resources.appendingPathComponent("runtime").path
            environment["PYTHONPATH"] = root.path
            environment["PYTHONNOUSERSITE"] = "1"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment.removeValue(forKey: "PYTHONINSPECT")
        }
        return environment
    }
}
