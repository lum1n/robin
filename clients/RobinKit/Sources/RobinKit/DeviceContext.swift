import Foundation

/// Where the device is, coarsely. The house turns every value into a local reference before the model sees it.
public struct DeviceLocation: Encodable, Sendable, Equatable {
    public var locality: String?
    public var region: String?
    public var country: String?
    public var latitude: Double?
    public var longitude: Double?
    /// ISO 3166 alpha-2, used only by the house to look up public holidays.
    public var countryCode: String?

    public init(
        locality: String? = nil,
        region: String? = nil,
        country: String? = nil,
        latitude: Double? = nil,
        longitude: Double? = nil,
        countryCode: String? = nil
    ) {
        self.locality = locality
        self.region = region
        self.country = country
        self.countryCode = countryCode
        // About 1 km is enough for weather or "near me" and keeps the fix coarse on the wire.
        self.latitude = latitude.map { ($0 * 100).rounded() / 100 }
        self.longitude = longitude.map { ($0 * 100).rounded() / 100 }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encodeIfPresent(locality, forKey: .locality)
        try container.encodeIfPresent(region, forKey: .region)
        try container.encodeIfPresent(country, forKey: .country)
        try container.encodeIfPresent(latitude, forKey: .latitude)
        try container.encodeIfPresent(longitude, forKey: .longitude)
        try container.encodeIfPresent(countryCode, forKey: .countryCode)
    }

    enum CodingKeys: String, CodingKey {
        case locality, region, country, latitude, longitude
        case countryCode = "country_code"
    }
}

/// Runtime facts sent with each message so Robin knows "today", "here", and local formats from the person's device.
public struct DeviceContext: Encodable, Sendable, Equatable {
    public var timezone: String
    public var locale: String
    public var device: String
    public var units: String
    public var currency: String?
    public var location: DeviceLocation?

    public init(
        timezone: String,
        locale: String,
        device: String,
        units: String,
        currency: String? = nil,
        location: DeviceLocation? = nil
    ) {
        self.timezone = timezone
        self.locale = locale
        self.device = device
        self.units = units
        self.currency = currency
        self.location = location
    }

    /// The current device's time zone, locale, form factor, units, and currency, plus an optional location fix.
    /// Pass `device` ("iphone", "ipad", or "mac") when the app knows its idiom; RobinKit stays UIKit-free.
    public static func current(device: String? = nil, location: DeviceLocation? = nil) -> DeviceContext {
        let locale = Locale.current
        return DeviceContext(
            timezone: TimeZone.current.identifier,
            locale: locale.identifier,
            device: device ?? defaultDevice(),
            units: locale.measurementSystem == .metric ? "metric" : "imperial",
            currency: locale.currency?.identifier,
            location: location
        )
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(timezone, forKey: .timezone)
        try container.encode(locale, forKey: .locale)
        try container.encode(device, forKey: .device)
        try container.encode(units, forKey: .units)
        try container.encodeIfPresent(currency, forKey: .currency)
        try container.encodeIfPresent(location, forKey: .location)
    }

    enum CodingKeys: String, CodingKey {
        case timezone, locale, device, units, currency, location
    }

    private static func defaultDevice() -> String {
        #if os(iOS)
        return ProcessInfo.processInfo.isiOSAppOnMac ? "mac" : "iphone"
        #else
        return "mac"
        #endif
    }
}
