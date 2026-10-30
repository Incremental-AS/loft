// swift-tools-version:5.10
// Foundation-only on purpose: this package builds and tests on Linux
// as well as macOS. Everything AppKit/FileProvider-specific
// lives in the app and extension targets, which need the Mac.
import PackageDescription

let package = Package(
    name: "LoftCore",
    platforms: [.macOS(.v14), .iOS(.v17)],
    products: [.library(name: "LoftCore", targets: ["LoftCore"])],
    targets: [
        .target(name: "LoftCore"),
        .testTarget(name: "LoftCoreTests", dependencies: ["LoftCore"]),
    ]
)
