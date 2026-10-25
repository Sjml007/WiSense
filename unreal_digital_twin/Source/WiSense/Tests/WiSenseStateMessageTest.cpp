#include "CoreMinimal.h"

#if WITH_DEV_AUTOMATION_TESTS

#include "Misc/AutomationTest.h"
#include "WiSenseStateMessage.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Dom/JsonObject.h"

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FWiSenseStateMessageContractTest,
	"WiSense.Contract.StateMessageVerification",
	EAutomationTestFlags::ApplicationContextMask | EAutomationTestFlags::ProductFilter
)

bool FWiSenseStateMessageContractTest::RunTest(const FString& Parameters)
{
	// -------------------------------------------------------------------------
	// 1. Verify ComputeDisplayState display rules
	// -------------------------------------------------------------------------
	{
		// Rule 1: Active motion with confident zone assignment
		FWiSenseState ActiveState;
		ActiveState.SchemaVersion = 1;
		ActiveState.Timestamp = 100.0;
		ActiveState.bIsMotion = true;
		ActiveState.bPresence = true;
		ActiveState.ZoneId = 2;
		ActiveState.SeparationMargin = 0.05;
		ActiveState.CalibrationVersion = 1;

		FWiSenseDisplayState ActiveDisplay = ComputeDisplayState(ActiveState);
		TestTrue(TEXT("Active state: active zone is set"), ActiveDisplay.ActiveZone.IsSet());
		if (ActiveDisplay.ActiveZone.IsSet())
		{
			TestEqual(TEXT("Active state: zone ID is 2"), ActiveDisplay.ActiveZone.GetValue(), 2);
		}
		TestTrue(TEXT("Active state: room motion is true"), ActiveDisplay.bRoomMotion);
		TestTrue(TEXT("Active state: room presence is true"), ActiveDisplay.bRoomPresence);
		TestFalse(TEXT("Active state: motion with unknown zone is false"), ActiveDisplay.bMotionWithUnknownZone);

		// Rule 2: Ambiguous motion (motion active, zone null)
		FWiSenseState AmbiguousState;
		AmbiguousState.SchemaVersion = 1;
		AmbiguousState.Timestamp = 101.0;
		AmbiguousState.bIsMotion = true;
		AmbiguousState.bPresence = true;
		AmbiguousState.ZoneId.Reset();
		AmbiguousState.SeparationMargin.Reset();

		FWiSenseDisplayState AmbiguousDisplay = ComputeDisplayState(AmbiguousState);
		TestFalse(TEXT("Ambiguous state: active zone is not set"), AmbiguousDisplay.ActiveZone.IsSet());
		TestTrue(TEXT("Ambiguous state: room motion is true"), AmbiguousDisplay.bRoomMotion);
		TestTrue(TEXT("Ambiguous state: room presence is true"), AmbiguousDisplay.bRoomPresence);
		TestTrue(TEXT("Ambiguous state: motion with unknown zone is true"), AmbiguousDisplay.bMotionWithUnknownZone);

		// Rule 3: Presence hold (motion false, presence true)
		FWiSenseState PresenceHoldState;
		PresenceHoldState.SchemaVersion = 1;
		PresenceHoldState.Timestamp = 102.0;
		PresenceHoldState.bIsMotion = false;
		PresenceHoldState.bPresence = true;
		PresenceHoldState.ZoneId.Reset();
		PresenceHoldState.SeparationMargin.Reset();

		FWiSenseDisplayState PresenceHoldDisplay = ComputeDisplayState(PresenceHoldState);
		TestFalse(TEXT("Presence hold: active zone is not set"), PresenceHoldDisplay.ActiveZone.IsSet());
		TestFalse(TEXT("Presence hold: room motion is false"), PresenceHoldDisplay.bRoomMotion);
		TestTrue(TEXT("Presence hold: room presence is true"), PresenceHoldDisplay.bRoomPresence);
		TestFalse(TEXT("Presence hold: motion with unknown zone is false"), PresenceHoldDisplay.bMotionWithUnknownZone);

		// Rule 4: Quiet idle state (motion false, presence false)
		FWiSenseState QuietState;
		QuietState.SchemaVersion = 1;
		QuietState.Timestamp = 103.0;
		QuietState.bIsMotion = false;
		QuietState.bPresence = false;
		QuietState.ZoneId.Reset();
		QuietState.SeparationMargin.Reset();

		FWiSenseDisplayState QuietDisplay = ComputeDisplayState(QuietState);
		TestFalse(TEXT("Quiet state: active zone is not set"), QuietDisplay.ActiveZone.IsSet());
		TestFalse(TEXT("Quiet state: room motion is false"), QuietDisplay.bRoomMotion);
		TestFalse(TEXT("Quiet state: room presence is false"), QuietDisplay.bRoomPresence);
		TestFalse(TEXT("Quiet state: motion with unknown zone is false"), QuietDisplay.bMotionWithUnknownZone);
	}

	// -------------------------------------------------------------------------
	// 2. Table-driven test using shared contract fixtures and manifest.json
	// -------------------------------------------------------------------------
	FString ManifestPath = FPaths::Combine(FPaths::ProjectDir(), TEXT("../contracts/fixtures/state_message/manifest.json"));
	FPaths::CollapseRelativeDirectories(ManifestPath);

	FString ManifestJson;
	if (!FFileHelper::LoadFileToString(ManifestJson, *ManifestPath))
	{
		AddError(FString::Printf(TEXT("Failed to load fixture manifest at: %s"), *ManifestPath));
		return false;
	}

	TSharedPtr<FJsonObject> ManifestObj;
	TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(ManifestJson);
	if (!FJsonSerializer::Deserialize(Reader, ManifestObj) || !ManifestObj.IsValid())
	{
		AddError(TEXT("Failed to parse fixture manifest JSON"));
		return false;
	}

	const TArray<TSharedPtr<FJsonValue>>* FixturesArray = nullptr;
	if (!ManifestObj->TryGetArrayField(TEXT("fixtures"), FixturesArray) || !FixturesArray)
	{
		AddError(TEXT("Manifest missing 'fixtures' array"));
		return false;
	}

	const FString FixturesDir = FPaths::GetPath(ManifestPath);

	for (const TSharedPtr<FJsonValue>& FixtureVal : *FixturesArray)
	{
		const TSharedPtr<FJsonObject> FixtureObj = FixtureVal->AsObject();
		if (!FixtureObj.IsValid())
		{
			continue;
		}

		const FString FixtureName = FixtureObj->GetStringField(TEXT("name"));
		const FString FixtureFile = FixtureObj->GetStringField(TEXT("file"));
		const bool bExpectedUnreal = FixtureObj->GetBoolField(TEXT("expected_unreal"));

		const FString FixtureFilePath = FPaths::Combine(FixturesDir, FixtureFile);
		FString FixtureContent;
		if (!FFileHelper::LoadFileToString(FixtureContent, *FixtureFilePath))
		{
			AddError(FString::Printf(TEXT("Failed to load fixture file: %s"), *FixtureFilePath));
			continue;
		}

		FWiSenseState State;
		FString ParseError;
		const bool bParseResult = FWiSenseState::ParseStateMessage(FixtureContent, State, ParseError);

		TestEqual(
			FString::Printf(TEXT("Fixture [%s] Parse Result matches expected_unreal (Error: %s)"), *FixtureName, *ParseError),
			bParseResult,
			bExpectedUnreal
		);

		// If expected to pass in Unreal, verify invariants on the parsed struct
		if (bExpectedUnreal && bParseResult)
		{
			TestEqual(TEXT("schema_version is 1"), State.SchemaVersion, 1);
			TestTrue(TEXT("timestamp is positive"), State.Timestamp > 0.0);
			if (!State.bIsMotion)
			{
				TestFalse(TEXT("zone_id is null when is_motion is false"), State.ZoneId.IsSet());
				TestFalse(TEXT("separation_margin is null when is_motion is false"), State.SeparationMargin.IsSet());
			}
			if (State.ZoneId.IsSet())
			{
				TestTrue(TEXT("zone_id is >= 1"), State.ZoneId.GetValue() >= 1);
			}
		}
	}

	return true;
}

#endif // WITH_DEV_AUTOMATION_TESTS
