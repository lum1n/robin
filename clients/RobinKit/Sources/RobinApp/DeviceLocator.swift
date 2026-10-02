import CoreLocation
import Foundation
import RobinKit

#if os(iOS)
import UIKit
#endif

/// Keeps a coarse, reverse-geocoded fix for message context. Only asks when the app declares why it needs location.
@MainActor
final class DeviceLocator: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private let geocoder = CLGeocoder()
    private(set) var latest: DeviceLocation?
    private var lastGeocoded: CLLocation?

    /// SwiftPM builds have no Info.plist; without the usage string, requesting would crash or be ignored.
    static var canAsk: Bool {
        Bundle.main.object(forInfoDictionaryKey: "NSLocationWhenInUseUsageDescription") != nil
    }

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyKilometer
        manager.distanceFilter = 1_000
    }

    func start() {
        guard Self.canAsk, CLLocationManager.locationServicesEnabled() else { return }
        switch manager.authorizationStatus {
        case .notDetermined:
            manager.requestWhenInUseAuthorization()
        case .denied, .restricted:
            latest = nil
        default:
            manager.requestLocation()
        }
    }

    func context() -> DeviceContext {
        start()
        return DeviceContext.current(device: Self.device, location: latest)
    }

    private static var device: String {
        #if os(iOS)
        if ProcessInfo.processInfo.isiOSAppOnMac { return "mac" }
        return UIDevice.current.userInterfaceIdiom == .pad ? "ipad" : "iphone"
        #else
        return "mac"
        #endif
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        Task { @MainActor in
            switch status {
            case .denied, .restricted:
                self.latest = nil
            case .notDetermined:
                break
            default:
                self.manager.requestLocation()
            }
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let fix = locations.last else { return }
        Task { @MainActor in self.update(fix) }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {}

    private func update(_ fix: CLLocation) {
        let coordinate = fix.coordinate
        let place = latest
        latest = DeviceLocation(
            locality: place?.locality,
            region: place?.region,
            country: place?.country,
            latitude: coordinate.latitude,
            longitude: coordinate.longitude,
            countryCode: place?.countryCode
        )
        if let previous = lastGeocoded, previous.distance(from: fix) < 1_000, place?.locality != nil { return }
        lastGeocoded = fix
        geocoder.reverseGeocodeLocation(fix) { [weak self] marks, _ in
            guard let mark = marks?.first else { return }
            let locality = mark.locality
            let region = mark.administrativeArea
            let country = mark.country
            let countryCode = mark.isoCountryCode
            Task { @MainActor in
                guard let self else { return }
                self.latest = DeviceLocation(
                    locality: locality,
                    region: region,
                    country: country,
                    latitude: coordinate.latitude,
                    longitude: coordinate.longitude,
                    countryCode: countryCode
                )
            }
        }
    }
}
