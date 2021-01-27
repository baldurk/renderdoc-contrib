#version 420 core

#if defined(VULKAN)

layout(binding = 0, std140) uniform RENDERDOC_Uniforms
{
	uvec4 TexDim;
	uint SelectedMip;
	int TextureType; // 1 = 1D, 2 = 2D, 3 = 3D, 4 = 2DMS
	uint SelectedSliceFace;
	int SelectedSample;
	uvec4 YUVDownsampleRate;
	uvec4 YUVAChannels;
} RENDERDOC;

#define RENDERDOC_TexDim RENDERDOC.TexDim
#define RENDERDOC_SelectedMip RENDERDOC.SelectedMip
#define RENDERDOC_TextureType RENDERDOC.TextureType
#define RENDERDOC_SelectedSliceFace RENDERDOC.SelectedSliceFace
#define RENDERDOC_SelectedSample RENDERDOC.SelectedSample
#define RENDERDOC_YUVDownsampleRate RENDERDOC.YUVDownsampleRate
#define RENDERDOC_YUVAChannels RENDERDOC.YUVAChannels

// Textures
// Floating point samplers
layout(binding = 6) uniform sampler1DArray tex1DArray;
layout(binding = 7) uniform sampler2DArray tex2DArray;
layout(binding = 8) uniform sampler3D tex3D;
layout(binding = 9) uniform sampler2DMSArray tex2DMSArray;

/*
// Unsigned int samplers
layout(binding = 11) uniform usampler1DArray texUInt1DArray;
layout(binding = 12) uniform usampler2DArray texUInt2DArray;
layout(binding = 13) uniform usampler3D texUInt3D;
layout(binding = 14) uniform usampler2DMSArray texUInt2DMSArray;

// Int samplers
layout(binding = 16) uniform isampler1DArray texSInt1DArray;
layout(binding = 17) uniform isampler2DArray texSInt2DArray;
layout(binding = 18) uniform isampler3D texSInt3D;
layout(binding = 19) uniform isampler2DMSArray texSInt2DMSArray;
*/

// End Textures

vec4 get_pixel_col(vec2 uv)
{
  vec4 col = vec4(0,0,0,0);

  uvec4 texRes = RENDERDOC_TexDim;

  // RENDERDOC_TexDim is always the dimension of the texture. When loading from smaller mips, we need to multiply
  // uv by the mip dimension
  texRes.x = max(1u, texRes.x >> RENDERDOC_SelectedMip);
  texRes.y = max(1u, texRes.y >> RENDERDOC_SelectedMip);

  if(RENDERDOC_TextureType == 1)
  {
    col = texelFetch(tex1DArray, ivec2(uv.x * texRes.x, RENDERDOC_SelectedSliceFace), int(RENDERDOC_SelectedMip));
  }
  else if(RENDERDOC_TextureType == 2)
  {
    col = texelFetch(tex2DArray, ivec3(uv * texRes.xy, RENDERDOC_SelectedSliceFace), int(RENDERDOC_SelectedMip));
  }
  else if(RENDERDOC_TextureType == 3)
  {
    col = texelFetch(tex3D, ivec3(uv * texRes.xy, RENDERDOC_SelectedSliceFace), int(RENDERDOC_SelectedMip));
  }
  else if(RENDERDOC_TextureType == 4)
  {
    if(RENDERDOC_SelectedSample < 0)
    {
      int sampleCount = -RENDERDOC_SelectedSample;

      // worst resolve you've seen in your life
      for(int i = 0; i < sampleCount; i++)
        col += texelFetch(tex2DMSArray, ivec3(uv * texRes.xy, RENDERDOC_SelectedSliceFace), i);

      col /= float(sampleCount);
    }
    else
    {
      col = texelFetch(tex2DMSArray, ivec3(uv * texRes.xy, RENDERDOC_SelectedSliceFace), RENDERDOC_SelectedSample);
    }
  }


  return col;
}

#else

// OpenGL


// Textures
/*
// Unsigned int samplers
layout (binding = 1) uniform usampler1D texUInt1D;
layout (binding = 2) uniform usampler2D texUInt2D;
layout (binding = 3) uniform usampler3D texUInt3D;
// cube = 4
layout (binding = 5) uniform usampler1DArray texUInt1DArray;
layout (binding = 6) uniform usampler2DArray texUInt2DArray;
// cube array = 7
layout (binding = 8) uniform usampler2DRect texUInt2DRect;
layout (binding = 9) uniform usamplerBuffer texUIntBuffer;
layout (binding = 10) uniform usampler2DMS texUInt2DMS;
layout (binding = 11) uniform usampler2DMSArray texUInt2DMSArray;

// Int samplers
layout (binding = 1) uniform isampler1D texSInt1D;
layout (binding = 2) uniform isampler2D texSInt2D;
layout (binding = 3) uniform isampler3D texSInt3D;
// cube = 4
layout (binding = 5) uniform isampler1DArray texSInt1DArray;
layout (binding = 6) uniform isampler2DArray texSInt2DArray;
// cube array = 7
layout (binding = 8) uniform isampler2DRect texSInt2DRect;
layout (binding = 9) uniform isamplerBuffer texSIntBuffer;
layout (binding = 10) uniform isampler2DMS texSInt2DMS;
layout (binding = 11) uniform isampler2DMSArray texSInt2DMSArray;
*/

// Floating point samplers
layout (binding = 1) uniform sampler1D tex1D;
layout (binding = 2) uniform sampler2D tex2D;
layout (binding = 3) uniform sampler3D tex3D;
layout (binding = 4) uniform samplerCube texCube;
layout (binding = 5) uniform sampler1DArray tex1DArray;
layout (binding = 6) uniform sampler2DArray tex2DArray;
layout (binding = 7) uniform samplerCubeArray texCubeArray;
layout (binding = 8) uniform sampler2DRect tex2DRect;
layout (binding = 9) uniform samplerBuffer texBuffer;
layout (binding = 10) uniform sampler2DMS tex2DMS;
layout (binding = 11) uniform sampler2DMSArray tex2DMSArray;
// End Textures

// 1 = 1D, 2 = 2D, 3 = 3D, 4 = Cube
// 5 = 1DArray, 6 = 2DArray, 7 = CubeArray
// 8 = Rect, 9 = Buffer, 10 = 2DMS, 11 = 2DMSArray
uniform uint RENDERDOC_TextureType;

// selected MSAA sample or -numSamples for resolve. See docs
uniform int RENDERDOC_SelectedSample;

// selected array slice or cubemap face in UI
uniform uint RENDERDOC_SelectedSliceFace;

// selected mip in UI
uniform uint RENDERDOC_SelectedMip;

// xyz == width, height, depth. w == # mips
uniform uvec4 RENDERDOC_TexDim;

vec4 get_pixel_col(vec2 uv)
{
  vec4 col = vec4(0,0,0,0);

  uvec4 texRes = RENDERDOC_TexDim;

  // RENDERDOC_TexDim is always the dimension of the texture. When loading from smaller mips, we need to multiply
  // uv by the mip dimension
  texRes.x = max(1u, texRes.x >> RENDERDOC_SelectedMip);
  texRes.y = max(1u, texRes.y >> RENDERDOC_SelectedMip);

  if(RENDERDOC_TextureType == 1)
  {
    col = texelFetch(tex1D, int(uv.x * texRes.x), int(RENDERDOC_SelectedMip));
  }
  else if(RENDERDOC_TextureType == 2)
  {
    col = texelFetch(tex2D, ivec2(uv * texRes.xy), int(RENDERDOC_SelectedMip));
  }
  else if(RENDERDOC_TextureType == 3)
  {
    col = texelFetch(tex3D, ivec3(uv * texRes.xy, RENDERDOC_SelectedSliceFace), int(RENDERDOC_SelectedMip));
  }
  else if(RENDERDOC_TextureType == 4)
  {
    // don't handle cubemaps here, GL needs you to generate a cubemap lookup vector
  }
  else if(RENDERDOC_TextureType == 5)
  {
    col = texelFetch(tex1DArray, ivec2(uv.x * texRes.x, RENDERDOC_SelectedSliceFace), int(RENDERDOC_SelectedMip));
  }
  else if(RENDERDOC_TextureType == 6)
  {
    col = texelFetch(tex2DArray, ivec3(uv * texRes.xy, RENDERDOC_SelectedSliceFace), int(RENDERDOC_SelectedMip));
  }
  else if(RENDERDOC_TextureType == 7)
  {
    // don't handle cubemaps here, GL needs you to generate a cubemap lookup vector
  }
  else if(RENDERDOC_TextureType == 8)
  {
    col = texelFetch(tex2DRect, ivec2(uv * texRes.xy));
  }
  else if(RENDERDOC_TextureType == 9)
  {
    col = texelFetch(texBuffer, int(uv.x * texRes.x));
  }
  else if(RENDERDOC_TextureType == 10)
  {
    if(RENDERDOC_SelectedSample < 0)
    {
      int sampleCount = -RENDERDOC_SelectedSample;

      // worst resolve you've seen in your life
      for(int i = 0; i < sampleCount; i++)
        col += texelFetch(tex2DMS, ivec2(uv * texRes.xy), i);

      col /= float(sampleCount);
    }
    else
    {
      col = texelFetch(tex2DMS, ivec2(uv * texRes.xy), RENDERDOC_SelectedSample);
    }
  }
  else if(RENDERDOC_TextureType == 11)
  {
    if(RENDERDOC_SelectedSample < 0)
    {
      int sampleCount = -RENDERDOC_SelectedSample;

      // worst resolve you've seen in your life
      for(int i = 0; i < sampleCount; i++)
        col += texelFetch(tex2DMSArray, ivec3(uv * texRes.xy, RENDERDOC_SelectedSliceFace), i);

      col /= float(sampleCount);
    }
    else
    {
      col = texelFetch(tex2DMSArray, ivec3(uv * texRes.xy, RENDERDOC_SelectedSliceFace), RENDERDOC_SelectedSample);
    }
  }

  return col;
}

#endif

layout (location = 0) in vec2 uv;
layout (location = 0) out vec4 color_out;

void main()
{
  vec4 col = get_pixel_col(uv.xy);

  col.rgb = vec3(1.0f, 1.0f, 1.0f) - col.rgb;

  color_out = col;
}
