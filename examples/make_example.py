"""Generate two original calibration cuboids; no third-party CAD assets."""
from pathlib import Path
import struct


def cuboid(path, size=(20, 20, 10), offset=(0, 0, 0), omit_last=False):
    vertices = [(0,0,0),(1,0,0),(1,1,0),(0,1,0),(0,0,1),(1,0,1),(1,1,1),(0,1,1)]
    faces = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),(1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
    if omit_last: faces.pop()
    with Path(path).open('wb') as stream:
        stream.write(b'Print Dispatch original calibration cuboid'.ljust(80,b' '))
        stream.write(struct.pack('<I',len(faces)))
        for face in faces:
            xyz = [offset[i]+vertices[index][i]*size[i] for index in face for i in range(3)]
            stream.write(struct.pack('<12fH',0,0,0,*xyz,0))


if __name__ == '__main__':
    folder=Path(__file__).parent
    cuboid(folder/'calibration-pad.stl',(40,20,5))
    cuboid(folder/'clearance-block.stl',(12,12,10))
