#include "Modules/ModuleManager.h"

#if PLATFORM_WINDOWS && UE_BUILD_DEVELOPMENT && !WITH_EDITOR
#include "Engine/GameViewportClient.h"
#include "Engine/LocalPlayer.h"
#include "Engine/World.h"
#include "EnhancedActionKeyMapping.h"
#include "EnhancedInputSubsystems.h"
#include "EnhancedPlayerInput.h"
#include "GameFramework/Character.h"
#include "GameFramework/CharacterMovementComponent.h"
#include "GameFramework/PlayerController.h"
#include "GenericPlatform/GenericApplication.h"
#include "GenericPlatform/GenericPlatformInputDeviceMapper.h"
#include "HAL/IConsoleManager.h"
#include "HAL/PlatformApplicationMisc.h"
#include "InputAction.h"
#include "InputKeyEventArgs.h"
#include "InputMappingContext.h"
#include "Misc/CommandLine.h"
#include "Misc/Parse.h"
#include "UObject/UObjectIterator.h"
#include "UnrealClient.h"
#include "Widgets/SWindow.h"

DEFINE_LOG_CATEGORY_STATIC(LogBackgroundInputProbe, Log, All);

class FBackgroundInputProbe : public IModuleInterface
{
    void StartupModule() override
    {
        FString Mode;
        FParse::Value(FCommandLine::Get(), TEXT("QAInputProbe="), Mode);
        bMove = Mode == TEXT("move") || Mode == TEXT("combo") || Mode == TEXT("cancel");
        bJump = Mode == TEXT("jump") || Mode == TEXT("combo");
        bCancel = Mode == TEXT("cancel");
        bForced = FParse::Param(FCommandLine::Get(), TEXT("QAForcedKey"));
        if (bMove || bJump)
        {
            StartHandle = FWorldDelegates::OnWorldTickStart.AddRaw(this, &FBackgroundInputProbe::BeforeTick);
            EndHandle = FWorldDelegates::OnWorldPostActorTick.AddRaw(this, &FBackgroundInputProbe::AfterTick);
            CleanupHandle = FWorldDelegates::OnWorldCleanup.AddRaw(this, &FBackgroundInputProbe::Cleanup);
        }
    }

    void ShutdownModule() override
    {
        Release();
        FWorldDelegates::OnWorldTickStart.Remove(StartHandle);
        FWorldDelegates::OnWorldPostActorTick.Remove(EndHandle);
        FWorldDelegates::OnWorldCleanup.Remove(CleanupHandle);
    }

    void Cleanup(UWorld* World, bool, bool)
    {
        if (RunWorld.Get() == World)
        {
            Release();
            bFinished = true;
        }
    }

    void BeforeTick(UWorld* World, ELevelTick, float)
    {
        if (bFinished || !World->IsGameWorld() || (RunWorld.IsValid() && RunWorld.Get() != World))
        {
            return;
        }
        const double Now = FPlatformTime::Seconds();
        if (bDown && Now >= ReleaseAt)
        {
            Release();
        }
        if (RunWorld.IsValid())
        {
            return;
        }

        APlayerController* PC = World->GetFirstPlayerController();
        ULocalPlayer* LP = PC ? PC->GetLocalPlayer() : nullptr;
        ACharacter* Character = PC ? Cast<ACharacter>(PC->GetPawn()) : nullptr;
        UEnhancedPlayerInput* Input = PC ? Cast<UEnhancedPlayerInput>(PC->PlayerInput) : nullptr;
        UGameViewportClient* Client = LP ? LP->ViewportClient : nullptr;
        if (!Character || !Input || !Client || !Client->Viewport)
        {
            return;
        }
        if (StartAt == 0.0)
        {
            StartAt = Now + 10.0;
            UE_LOG(LogBackgroundInputProbe, Display, TEXT("Armed: input in 10 seconds"));
        }
        if (Now < StartAt)
        {
            return;
        }

        if (const auto* Subsystem = ULocalPlayer::GetSubsystem<UEnhancedInputLocalPlayerSubsystem>(LP))
        {
            for (TObjectIterator<UInputMappingContext> Context; Context; ++Context)
            {
                int32 Priority = 0;
                if (Subsystem->HasMappingContext(*Context, Priority))
                {
                    UE_LOG(LogBackgroundInputProbe, Display, TEXT("IMC %s priority=%d"), *Context->GetName(), Priority);
                }
            }
        }
        for (const FEnhancedActionKeyMapping& Mapping : Input->GetEnhancedActionMappingsView())
        {
            if (Mapping.Key == EKeys::W && GetNameSafe(Mapping.Action.Get()) == TEXT("IA_Move"))
            {
                MoveAction = Mapping.Action.Get();
            }
            if (Mapping.Key == EKeys::SpaceBar && GetNameSafe(Mapping.Action.Get()) == TEXT("IA_Jump"))
            {
                JumpAction = Mapping.Action.Get();
            }
        }
        Device = IPlatformInputDeviceMapper::Get().GetPrimaryInputDeviceForUser(LP->GetPlatformUserId());
        if ((bMove && !MoveAction.IsValid()) || (bJump && !JumpAction.IsValid()) || (!bForced && !Device.IsValid()))
        {
            UE_LOG(LogBackgroundInputProbe, Error, TEXT("Required Third Person mapping or input device missing"));
            bFinished = true;
            return;
        }
        RunWorld = World;
        Viewport = Client;
        PlayerInput = Input;
        Pawn = Character;
        InitialLocation = Character->GetActorLocation();
        Forward = FRotator(0.0, PC->GetControlRotation().Yaw, 0.0).Vector();
        UE_LOG(LogBackgroundInputProbe, Display, TEXT("Target player=%s controller=%s pawn=%s start=%s grounded=%d"),
            *LP->GetName(), *PC->GetName(), *Character->GetName(), *InitialLocation.ToCompactString(),
            Character->GetCharacterMovement()->IsMovingOnGround());
        // The cancel case interrupts the 800 ms hold after 200 ms.
        ReleaseAt = Now + (bCancel ? 0.2 : 0.8);
        ObserveUntil = ReleaseAt + 0.75;
        bDown = true;
        SendKeys(true);
    }

    void SendKeys(bool bPress)
    {
        UGameViewportClient* Client = Viewport.Get();
        for (const FKey& Key : {EKeys::W, EKeys::SpaceBar})
        {
            if ((Key == EKeys::W && !bMove) || (Key == EKeys::SpaceBar && !bJump))
            {
                continue;
            }
            bool bHandled = false;
            if (bForced && RunWorld.IsValid())
            {
                const FString Command = FString::Printf(TEXT("Input.%ckey %s"),
                    bPress ? TCHAR('+') : TCHAR('-'), *Key.GetFName().ToString());
                bHandled = IConsoleManager::Get().ProcessUserConsoleInput(*Command, *GLog, RunWorld.Get());
            }
            else if (Client && Client->Viewport)
            {
                bHandled = Client->InputKey(FInputKeyEventArgs::CreateSimulated(
                    Key, bPress ? IE_Pressed : IE_Released, bPress ? 1.0f : 0.0f,
                    -1, Device, false, Client->Viewport));
            }
            UE_LOG(LogBackgroundInputProbe, Display, TEXT("L1 frame=%llu time=%.3f path=%s key=%s event=%s handled=%d"),
                GFrameCounter, FPlatformTime::Seconds(), bForced ? TEXT("forced") : TEXT("viewport"), *Key.GetFName().ToString(),
                bPress ? TEXT("down") : TEXT("up"), bHandled);
        }
    }

    void Release()
    {
        if (bDown)
        {
            SendKeys(false);
            bDown = false;
        }
    }

    void AfterTick(UWorld* World, ELevelTick, float Delta)
    {
        if (bFinished || RunWorld.Get() != World)
        {
            return;
        }
        const ACharacter* Character = Pawn.Get();
        UEnhancedPlayerInput* Input = PlayerInput.Get();
        if (!Character || !Input)
        {
            Release();
            bFinished = true;
            UE_LOG(LogBackgroundInputProbe, Error, TEXT("Probe target disappeared"));
            return;
        }
        const auto Trigger = [Input](const UInputAction* Action)
        {
            const FInputActionInstance* Instance = Action ? Input->FindActionInstanceData(Action) : nullptr;
            return Instance ? Instance->GetTriggerEvent() : ETriggerEvent::None;
        };
        const ETriggerEvent Move = Trigger(MoveAction.Get());
        const ETriggerEvent Jump = Trigger(JumpAction.Get());
        const bool bWPressed = Input->IsPressed(EKeys::W);
        const bool bSpacePressed = Input->IsPressed(EKeys::SpaceBar);
        UE_LOG(LogBackgroundInputProbe, Display, TEXT("L2 frame=%llu move=%d jump=%d W=%d SpaceBar=%d"),
            GFrameCounter, int32(Move), int32(Jump), bWPressed, bSpacePressed);
        const FVector Location = Character->GetActorLocation();
        const FVector Velocity = Character->GetVelocity();
        const bool bGrounded = Character->GetCharacterMovement()->IsMovingOnGround();
        const UGameViewportClient* Client = Viewport.Get();
        const TSharedPtr<const SWindow> Window = Client ? Client->GetWindow() : nullptr;
        const TSharedPtr<const FGenericWindow> Native = Window ? Window->GetNativeWindow() : nullptr;
        const bool bForeground = FPlatformApplicationMisc::IsThisApplicationForeground();
        UE_LOG(LogBackgroundInputProbe, Display,
            TEXT("L3 frame=%llu dt=%.4f foreground=%d viewportFocus=%d minimized=%d position=%s velocity=%s grounded=%d"),
            GFrameCounter, Delta, bForeground, Client && Client->Viewport && Client->Viewport->HasFocus(),
            Native ? int32(Native->IsMinimized()) : -1,
            *Location.ToCompactString(), *Velocity.ToCompactString(), bGrounded);

        bMoveTriggered |= Move == ETriggerEvent::Triggered;
        bJumpTriggered |= Jump == ETriggerEvent::Triggered;
        bMoved |= FVector::DotProduct(Location - InitialLocation, Forward) > 5.0;
        bRose |= !bGrounded && Velocity.Z > 0.0;
        if (bDown)
        {
            bSawWDown |= bWPressed;
            bSawSpaceDown |= bSpacePressed;
        }
        else
        {
            bResidualInput |= (bMove && bWPressed) || (bJump && bSpacePressed);
        }
        if (!bDown && FPlatformTime::Seconds() >= ObserveUntil)
        {
            const bool bPassed = !bResidualInput
                && (!bMove || (bSawWDown && bMoveTriggered && bMoved))
                && (!bJump || (bSawSpaceDown && bJumpTriggered && bRose));
            UE_LOG(LogBackgroundInputProbe, Display, TEXT("Probe result=%s cancel=%d residual=%d"),
                bPassed ? TEXT("PASS") : TEXT("FAIL"), bCancel, bResidualInput);
            bFinished = true;
        }
    }

    FDelegateHandle StartHandle, EndHandle, CleanupHandle;
    TWeakObjectPtr<UWorld> RunWorld;
    TWeakObjectPtr<UGameViewportClient> Viewport;
    TWeakObjectPtr<UEnhancedPlayerInput> PlayerInput;
    TWeakObjectPtr<ACharacter> Pawn;
    TWeakObjectPtr<const UInputAction> MoveAction, JumpAction;
    FInputDeviceId Device = INPUTDEVICEID_NONE;
    FVector InitialLocation = FVector::ZeroVector;
    FVector Forward = FVector::ZeroVector;
    double StartAt = 0.0, ReleaseAt = 0.0, ObserveUntil = 0.0;
    bool bMove = false, bJump = false, bCancel = false, bForced = false;
    bool bDown = false, bFinished = false;
    bool bMoveTriggered = false, bJumpTriggered = false, bMoved = false, bRose = false;
    bool bSawWDown = false, bSawSpaceDown = false, bResidualInput = false;
};

IMPLEMENT_MODULE(FBackgroundInputProbe, BackgroundInputProbe)
#else
IMPLEMENT_MODULE(FDefaultModuleImpl, BackgroundInputProbe)
#endif
