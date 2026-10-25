#include "WiSenseManager.h"

#include "Components/StaticMeshComponent.h"
#include "Engine/StaticMesh.h"
#include "WebSocketsModule.h"
#include "IWebSocket.h"
#include "HAL/PlatformTime.h"

AWiSenseManager::AWiSenseManager()
{
	PrimaryActorTick.bCanEverTick = false;
}

void AWiSenseManager::BeginPlay()
{
	Super::BeginPlay();

	SpawnZones();
	ConnectWebSocket();
}

void AWiSenseManager::EndPlay(const EEndPlayReason::Type EndPlayReason)
{
	if (WebSocket.IsValid())
	{
		WebSocket->Close();
		WebSocket.Reset();
	}

	ResetToIdle();

	Super::EndPlay(EndPlayReason);
}

void AWiSenseManager::SpawnZones()
{
	UStaticMesh* CubeMesh = LoadObject<UStaticMesh>(
		nullptr,
		TEXT("/Engine/BasicShapes/Cube.Cube")
	);

	if (!CubeMesh)
	{
		UE_LOG(LogTemp, Error, TEXT("WiSense: Failed to load cube mesh."));
		return;
	}

	// TODO(Section 18): Data-driven zone count, coordinates, and dimensions from config
	const FVector Locations[] =
	{
		FVector(-300.0f, -300.0f, 50.0f),
		FVector( 300.0f, -300.0f, 50.0f),
		FVector(-300.0f,  300.0f, 50.0f),
		FVector( 300.0f,  300.0f, 50.0f)
	};

	for (int32 i = 0; i < 4; ++i)
	{
		UStaticMeshComponent* Mesh = NewObject<UStaticMeshComponent>(this);

		Mesh->SetStaticMesh(CubeMesh);
		Mesh->SetRelativeLocation(Locations[i]);
		Mesh->SetRelativeScale3D(FVector(1.5f, 1.5f, 0.2f));

		Mesh->SetCollisionEnabled(ECollisionEnabled::QueryAndPhysics);

		// Section 17 fix: Start every zone hidden to resolve the initial-state bug
		Mesh->SetVisibility(false, true);

		Mesh->RegisterComponent();
		AddInstanceComponent(Mesh);

		const int32 ZoneID = i + 1;

		Zones.Add(ZoneID, Mesh);

		UE_LOG(
			LogTemp,
			Display,
			TEXT("WiSense: Spawned Zone %d (initially hidden)"),
			ZoneID
		);
	}
}

void AWiSenseManager::ConnectWebSocket()
{
	// TODO(Section 18): Move ServerURL and connection settings to configuration file
	const FString ServerURL = TEXT("ws://127.0.0.1:8765");
	const FString ServerProtocol = TEXT("ws");

	WebSocket = FWebSocketsModule::Get().CreateWebSocket(
		ServerURL,
		ServerProtocol
	);

	WebSocket->OnConnected().AddUObject(
		this,
		&AWiSenseManager::OnWebSocketConnected
	);

	WebSocket->OnConnectionError().AddUObject(
		this,
		&AWiSenseManager::OnWebSocketConnectionError
	);

	WebSocket->OnClosed().AddUObject(
		this,
		&AWiSenseManager::OnWebSocketClosed
	);

	WebSocket->OnMessage().AddUObject(
		this,
		&AWiSenseManager::OnWebSocketMessage
	);

	UE_LOG(
		LogTemp,
		Display,
		TEXT("WiSense: Connecting to %s"),
		*ServerURL
	);

	WebSocket->Connect();
}

void AWiSenseManager::OnWebSocketConnected()
{
	bConnected = true;

	UE_LOG(
		LogTemp,
		Display,
		TEXT("WiSense: WebSocket connected.")
	);
}

void AWiSenseManager::OnWebSocketConnectionError(const FString& Error)
{
	UE_LOG(
		LogTemp,
		Error,
		TEXT("WiSense: WebSocket connection error: %s"),
		*Error
	);

	ResetToIdle();
}

void AWiSenseManager::OnWebSocketClosed(
	int32 StatusCode,
	const FString& Reason,
	bool bWasClean
)
{
	UE_LOG(
		LogTemp,
		Warning,
		TEXT("WiSense: WebSocket closed. Code: %d Reason: %s Clean: %s"),
		StatusCode,
		*Reason,
		bWasClean ? TEXT("true") : TEXT("false")
	);

	ResetToIdle();
}

void AWiSenseManager::ResetToIdle()
{
	bConnected = false;

	// Hide all zones to avoid displaying stale data
	for (auto& Pair : Zones)
	{
		if (Pair.Value)
		{
			Pair.Value->SetVisibility(false, true);
		}
	}

	CurrentActiveZone.Reset();
	CurrentDisplayState = FWiSenseDisplayState();

	UE_LOG(
		LogTemp,
		Display,
		TEXT("WiSense: Reset to idle (all zones hidden, connection closed/idle).")
	);
}

void AWiSenseManager::OnWebSocketMessage(const FString& Message)
{
	ensure(IsInGameThread());

	UE_LOG(
		LogTemp,
		VeryVerbose,
		TEXT("WiSense: Received: %s"),
		*Message
	);

	FWiSenseState State;
	FString ParseError;

	if (!FWiSenseState::ParseStateMessage(Message, State, ParseError))
	{
		InvalidMessageCount++;

		// Throttle invalid-message errors to avoid log spam (at most once per second)
		const double Now = FPlatformTime::Seconds();
		if (Now - LastInvalidMessageErrorLogTime >= 1.0)
		{
			LastInvalidMessageErrorLogTime = Now;
			UE_LOG(
				LogTemp,
				Error,
				TEXT("WiSense: Invalid StateMessage (Total invalid: %llu): %s | Error: %s"),
				InvalidMessageCount,
				*Message,
				*ParseError
			);
		}
		return;
	}

	ValidMessageCount++;

	const FWiSenseDisplayState DisplayState = ComputeDisplayState(State);
	ApplyDisplayState(DisplayState);
}

void AWiSenseManager::ApplyDisplayState(const FWiSenseDisplayState& NewDisplayState)
{
	// Touch zone components only when the active zone changes
	if (NewDisplayState.ActiveZone != CurrentActiveZone)
	{
		// Hide previously active zone
		if (CurrentActiveZone.IsSet())
		{
			if (TObjectPtr<UStaticMeshComponent>* PrevPtr = Zones.Find(CurrentActiveZone.GetValue()))
			{
				if (*PrevPtr)
				{
					(*PrevPtr)->SetVisibility(false, true);
				}
			}
		}

		// Show newly active zone
		if (NewDisplayState.ActiveZone.IsSet())
		{
			if (TObjectPtr<UStaticMeshComponent>* NewPtr = Zones.Find(NewDisplayState.ActiveZone.GetValue()))
			{
				if (*NewPtr)
				{
					(*NewPtr)->SetVisibility(true, true);
				}
			}
			else
			{
				UE_LOG(
					LogTemp,
					Warning,
					TEXT("WiSense: Active zone %d not found in spawned zones map."),
					NewDisplayState.ActiveZone.GetValue()
				);
			}
		}

		CurrentActiveZone = NewDisplayState.ActiveZone;
	}

	// Log state changes at Display (motion, presence, zone, ambiguous state)
	if (NewDisplayState != CurrentDisplayState)
	{
		const FString ZoneStr = NewDisplayState.ActiveZone.IsSet()
			? FString::Printf(TEXT("Zone %d"), NewDisplayState.ActiveZone.GetValue())
			: TEXT("None");

		UE_LOG(
			LogTemp,
			Display,
			TEXT("WiSense: State changed -> ActiveZone: %s, Motion: %s, Presence: %s, AmbiguousMotion: %s (Valid msgs: %llu)"),
			*ZoneStr,
			NewDisplayState.bRoomMotion ? TEXT("true") : TEXT("false"),
			NewDisplayState.bRoomPresence ? TEXT("true") : TEXT("false"),
			NewDisplayState.bMotionWithUnknownZone ? TEXT("true") : TEXT("false"),
			ValidMessageCount
		);

		CurrentDisplayState = NewDisplayState;
	}
}