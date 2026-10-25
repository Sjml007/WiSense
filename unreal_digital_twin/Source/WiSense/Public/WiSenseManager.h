#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "WiSenseStateMessage.h"
#include "WiSenseManager.generated.h"

class IWebSocket;
class UStaticMeshComponent;

UCLASS()
class WISENSE_API AWiSenseManager : public AActor
{
	GENERATED_BODY()

public:
	AWiSenseManager();

protected:
	virtual void BeginPlay() override;
	virtual void EndPlay(const EEndPlayReason::Type EndPlayReason) override;

private:
	void SpawnZones();
	void ConnectWebSocket();
	void ResetToIdle();
	void ApplyDisplayState(const FWiSenseDisplayState& NewDisplayState);

	void OnWebSocketConnected();
	void OnWebSocketConnectionError(const FString& Error);
	void OnWebSocketClosed(int32 StatusCode, const FString& Reason, bool bWasClean);
	void OnWebSocketMessage(const FString& Message);

private:
	UPROPERTY()
	TMap<int32, TObjectPtr<UStaticMeshComponent>> Zones;

	TSharedPtr<IWebSocket> WebSocket;

	bool bConnected = false;
	TOptional<int32> CurrentActiveZone;
	FWiSenseDisplayState CurrentDisplayState;

	uint64 ValidMessageCount = 0;
	uint64 InvalidMessageCount = 0;
	double LastInvalidMessageErrorLogTime = 0.0;
};