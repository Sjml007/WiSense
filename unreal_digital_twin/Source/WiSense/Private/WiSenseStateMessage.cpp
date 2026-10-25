#include "WiSenseStateMessage.h"

#include "Dom/JsonObject.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Math/UnrealMathUtility.h"

FWiSenseDisplayState ComputeDisplayState(const FWiSenseState& State)
{
	FWiSenseDisplayState DisplayState;
	DisplayState.bRoomMotion = State.bIsMotion;
	DisplayState.bRoomPresence = State.bPresence;

	if (State.bIsMotion && State.ZoneId.IsSet())
	{
		DisplayState.ActiveZone = State.ZoneId.GetValue();
		DisplayState.bMotionWithUnknownZone = false;
	}
	else
	{
		DisplayState.ActiveZone.Reset();
		DisplayState.bMotionWithUnknownZone = (State.bIsMotion && !State.ZoneId.IsSet());
	}

	return DisplayState;
}

bool FWiSenseState::ParseStateMessage(const FString& JsonString, FWiSenseState& OutState, FString& OutError)
{
	TSharedPtr<FJsonObject> JsonObject;
	TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(JsonString);

	if (!FJsonSerializer::Deserialize(Reader, JsonObject) || !JsonObject.IsValid())
	{
		OutError = TEXT("Malformed JSON or payload is not a JSON object");
		return false;
	}

	// 1. schema_version: required and must equal 1
	const TSharedPtr<FJsonValue>* SchemaVerPtr = JsonObject->Values.Find(TEXT("schema_version"));
	if (!SchemaVerPtr || !SchemaVerPtr->IsValid() || (*SchemaVerPtr)->Type != EJson::Number)
	{
		OutError = TEXT("Missing or invalid 'schema_version' (must be a number)");
		return false;
	}
	const double SchemaVerNum = (*SchemaVerPtr)->AsNumber();
	if (SchemaVerNum != 1.0)
	{
		OutError = FString::Printf(TEXT("Unsupported schema_version (expected 1, got %g)"), SchemaVerNum);
		return false;
	}
	OutState.SchemaVersion = 1;

	// 2. timestamp: required and positive (> 0.0)
	const TSharedPtr<FJsonValue>* TsPtr = JsonObject->Values.Find(TEXT("timestamp"));
	if (!TsPtr || !TsPtr->IsValid() || (*TsPtr)->Type != EJson::Number)
	{
		OutError = TEXT("Missing or invalid 'timestamp' (must be a number)");
		return false;
	}
	const double Ts = (*TsPtr)->AsNumber();
	if (Ts <= 0.0 || !FMath::IsFinite(Ts))
	{
		OutError = FString::Printf(TEXT("Invalid 'timestamp' (must be positive and finite, got %f)"), Ts);
		return false;
	}
	OutState.Timestamp = Ts;

	// 3. is_motion: required real boolean
	const TSharedPtr<FJsonValue>* MotionPtr = JsonObject->Values.Find(TEXT("is_motion"));
	if (!MotionPtr || !MotionPtr->IsValid() || (*MotionPtr)->Type != EJson::Boolean)
	{
		OutError = TEXT("Missing or invalid 'is_motion' (must be a boolean)");
		return false;
	}
	OutState.bIsMotion = (*MotionPtr)->AsBool();

	// 4. presence: required real boolean
	const TSharedPtr<FJsonValue>* PresencePtr = JsonObject->Values.Find(TEXT("presence"));
	if (!PresencePtr || !PresencePtr->IsValid() || (*PresencePtr)->Type != EJson::Boolean)
	{
		OutError = TEXT("Missing or invalid 'presence' (must be a boolean)");
		return false;
	}
	OutState.bPresence = (*PresencePtr)->AsBool();

	// 5. zone_id: nullable, if present must be integer >= 1 (reject fractional, string, zero)
	const TSharedPtr<FJsonValue>* ZonePtr = JsonObject->Values.Find(TEXT("zone_id"));
	if (ZonePtr && ZonePtr->IsValid() && !(*ZonePtr)->IsNull())
	{
		if ((*ZonePtr)->Type != EJson::Number)
		{
			OutError = TEXT("Invalid 'zone_id' (must be a number or null)");
			return false;
		}
		const double ZoneNum = (*ZonePtr)->AsNumber();
		if (ZoneNum != FMath::FloorToDouble(ZoneNum))
		{
			OutError = FString::Printf(TEXT("Invalid fractional 'zone_id' (must be integer, got %f)"), ZoneNum);
			return false;
		}
		if (ZoneNum < 1.0)
		{
			OutError = FString::Printf(TEXT("Invalid 'zone_id' (must be >= 1, got %d)"), static_cast<int32>(ZoneNum));
			return false;
		}
		OutState.ZoneId = static_cast<int32>(ZoneNum);
	}
	else
	{
		OutState.ZoneId.Reset();
	}

	// 6. separation_margin: nullable, if present must be finite and >= 0.0
	const TSharedPtr<FJsonValue>* MarginPtr = JsonObject->Values.Find(TEXT("separation_margin"));
	if (MarginPtr && MarginPtr->IsValid() && !(*MarginPtr)->IsNull())
	{
		if ((*MarginPtr)->Type != EJson::Number)
		{
			OutError = TEXT("Invalid 'separation_margin' (must be a number or null)");
			return false;
		}
		const double MarginNum = (*MarginPtr)->AsNumber();
		if (!FMath::IsFinite(MarginNum) || MarginNum < 0.0)
		{
			OutError = FString::Printf(TEXT("Invalid 'separation_margin' (must be finite non-negative, got %f)"), MarginNum);
			return false;
		}
		OutState.SeparationMargin = MarginNum;
	}
	else
	{
		OutState.SeparationMargin.Reset();
	}

	// 7. calibration_version: nullable, if present must be integer >= 1
	const TSharedPtr<FJsonValue>* CalPtr = JsonObject->Values.Find(TEXT("calibration_version"));
	if (CalPtr && CalPtr->IsValid() && !(*CalPtr)->IsNull())
	{
		if ((*CalPtr)->Type != EJson::Number)
		{
			OutError = TEXT("Invalid 'calibration_version' (must be a number or null)");
			return false;
		}
		const double CalNum = (*CalPtr)->AsNumber();
		if (CalNum != FMath::FloorToDouble(CalNum) || CalNum < 1.0)
		{
			OutError = FString::Printf(TEXT("Invalid 'calibration_version' (must be integer >= 1, got %f)"), CalNum);
			return false;
		}
		OutState.CalibrationVersion = static_cast<int32>(CalNum);
	}
	else
	{
		OutState.CalibrationVersion.Reset();
	}

	// 8. Motion gating invariant: if is_motion is false then zone_id and separation_margin must be null
	if (!OutState.bIsMotion)
	{
		if (OutState.ZoneId.IsSet())
		{
			OutError = FString::Printf(TEXT("Motion gating violation: zone_id must be null when is_motion is false, got %d"), OutState.ZoneId.GetValue());
			return false;
		}
		if (OutState.SeparationMargin.IsSet())
		{
			OutError = FString::Printf(TEXT("Motion gating violation: separation_margin must be null when is_motion is false, got %f"), OutState.SeparationMargin.GetValue());
			return false;
		}
	}

	OutError.Empty();
	return true;
}
