#pragma once

#include "CoreMinimal.h"
#include "Misc/Optional.h"

/**
 * FWiSenseState
 * Plain struct representing the parsed StateMessage v1 payload from WiSense backend.
 * Nullable fields use TOptional.
 */
struct WISENSE_API FWiSenseState
{
	int32 SchemaVersion = 0;
	double Timestamp = 0.0;
	TOptional<int32> ZoneId;
	bool bIsMotion = false;
	bool bPresence = false;
	TOptional<double> SeparationMargin;
	TOptional<int32> CalibrationVersion;

	/**
	 * Parses a JSON StateMessage string into an FWiSenseState struct.
	 *
	 * Parsing Rules:
	 * 1. Valid JSON object.
	 * 2. schema_version is required and must equal 1.
	 * 3. timestamp is required and must be positive (> 0.0) and finite.
	 * 4. is_motion and presence must be real JSON booleans.
	 * 5. zone_id is nullable; if present, must be an integer >= 1 (reject fractional, string, and zero).
	 * 6. separation_margin is nullable; if present, must be finite and >= 0.0.
	 * 7. calibration_version is nullable; if present, must be integer >= 1.
	 * 8. Motion gating: if is_motion is false, zone_id and separation_margin must be null/absent.
	 *
	 * @param JsonString Raw JSON payload string.
	 * @param OutState Populated state struct on success.
	 * @param OutError Error description on failure.
	 * @return true if successfully parsed and compliant with wire contract; false otherwise.
	 */
	static bool ParseStateMessage(const FString& JsonString, FWiSenseState& OutState, FString& OutError);
};

/**
 * FWiSenseDisplayState
 * Pure derived spatial display state consumed by the Unreal digital twin.
 */
struct WISENSE_API FWiSenseDisplayState
{
	TOptional<int32> ActiveZone;
	bool bRoomMotion = false;
	bool bRoomPresence = false;
	bool bMotionWithUnknownZone = false;

	bool operator==(const FWiSenseDisplayState& Other) const
	{
		return ActiveZone == Other.ActiveZone
			&& bRoomMotion == Other.bRoomMotion
			&& bRoomPresence == Other.bRoomPresence
			&& bMotionWithUnknownZone == Other.bMotionWithUnknownZone;
	}

	bool operator!=(const FWiSenseDisplayState& Other) const
	{
		return !(*this == Other);
	}
};

/**
 * Pure function computing digital twin display state from a valid FWiSenseState.
 *
 * Rules:
 * - ActiveZone: Set ONLY when is_motion is true and ZoneId is set.
 * - bRoomMotion: Directly from State.bIsMotion.
 * - bRoomPresence: Directly from State.bPresence.
 * - bMotionWithUnknownZone: true if State.bIsMotion is true and ZoneId is NOT set.
 */
WISENSE_API FWiSenseDisplayState ComputeDisplayState(const FWiSenseState& State);

// Global helper forwarding to FWiSenseState::ParseStateMessage
inline bool ParseStateMessage(const FString& JsonString, FWiSenseState& OutState, FString& OutError)
{
	return FWiSenseState::ParseStateMessage(JsonString, OutState, OutError);
}
