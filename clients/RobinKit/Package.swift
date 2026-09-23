// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "RobinKit",
    products: [
        .library(name: "RobinKit", targets: ["RobinKit"]),
    ],
    targets: [
        .target(name: "RobinKit"),
        .testTarget(name: "RobinKitTests", dependencies: ["RobinKit"]),
    ]
)
