using UnrealBuildTool;

public class GameQABridge : ModuleRules
{
    public GameQABridge(ReadOnlyTargetRules Target) : base(Target)
    {
        PrivateDependencyModuleNames.AddRange(new[] {
            "Core", "CoreUObject", "Engine", "InputCore", "EnhancedInput", "ApplicationCore", "Boost", "Json"
        });
        PublicSystemLibraries.AddRange(new[] { "ws2_32.lib", "mswsock.lib", "bcrypt.lib" });
        bEnableExceptions = true;
    }
}
