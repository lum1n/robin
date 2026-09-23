// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "RobinKit",
    platforms: [
        .macOS(.v14),
        .iOS(.v17),
    ],
    products: [
        .library(name: "RobinKit", targets: ["RobinKit"]),
    ],
    targets: [
        .target(name: "RobinKit"),
        .testTarget(name: "RobinKitTests", dependencies: ["RobinKit"]),
    ]
)

#if os(macOS) || os(iOS)
package.products.append(.executable(name: "RobinApp", targets: ["RobinApp"]))
package.targets.append(.executableTarget(name: "RobinApp", dependencies: ["RobinKit"]))
#endif
