import Foundation

/// A household member. Stored encrypted on the house; the model only ever sees a reference and an age band.
public struct HouseholdMember: Codable, Sendable, Equatable, Identifiable {
    public var id = UUID()
    public var relation: String
    public var givenName: String
    public var familyName: String
    public var birthYear: Int?

    public static let relations = ["partner", "child", "parent", "sibling", "other"]

    public init(relation: String, givenName: String, familyName: String = "", birthYear: Int? = nil) {
        self.relation = relation
        self.givenName = givenName
        self.familyName = familyName
        self.birthYear = birthYear
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        relation = try container.decode(String.self, forKey: .relation)
        givenName = try container.decode(String.self, forKey: .givenName)
        familyName = try container.decodeIfPresent(String.self, forKey: .familyName) ?? ""
        birthYear = try container.decodeIfPresent(Int.self, forKey: .birthYear)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(relation, forKey: .relation)
        try container.encode(givenName, forKey: .givenName)
        if !familyName.isEmpty { try container.encode(familyName, forKey: .familyName) }
        try container.encodeIfPresent(birthYear, forKey: .birthYear)
    }

    enum CodingKeys: String, CodingKey {
        case relation
        case givenName = "given_name"
        case familyName = "family_name"
        case birthYear = "birth_year"
    }
}

/// A site Robin should try first for a topic, e.g. "football" → "nrk.no".
public struct PreferredSource: Codable, Sendable, Equatable, Identifiable {
    public var id = UUID()
    public var topic: String
    public var host: String

    public init(topic: String, host: String) {
        self.topic = topic
        self.host = host
    }

    enum CodingKeys: String, CodingKey { case topic, host }
}

public struct Preferences: Codable, Sendable, Equatable {
    public var style: String
    public var sources: [PreferredSource]

    public static let styles = ["auto", "concise", "detailed"]

    public init(style: String = "auto", sources: [PreferredSource] = []) {
        self.style = style
        self.sources = sources
    }
}

struct HouseholdBody: Codable {
    var accountID: String?
    var members: [HouseholdMember]

    enum CodingKeys: String, CodingKey {
        case accountID = "account_id"
        case members
    }
}

struct PreferencesUpdate: Encodable {
    var accountID: String
    var preferences: Preferences

    enum CodingKeys: String, CodingKey {
        case accountID = "account_id"
        case preferences
    }
}
